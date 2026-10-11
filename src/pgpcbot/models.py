"""Typed views of what the server sends: updates, messages, chats, people, keys and the bot.

Every model is a frozen dataclass. You can read its fields, compare two models and use one as a
dictionary key, but you cannot change it. Field names are the API's JSON names in Python style:
``from`` becomes ``from_user``, ``replyTo`` becomes ``reply_to``. Times are aware
`~datetime.datetime` objects in UTC; the server sends them as Unix milliseconds.

Each model made from JSON keeps that JSON, untouched, in ``raw``. So a field that a newer server
adds is never lost: this version has no attribute for it, but ``message.raw.get("someNewField")``
reads it. ``raw`` is left out of ``repr()``, of comparisons and of the hash. A model you build
by hand has an empty ``raw``.

The library builds the models for you. To build one yourself from the JSON of an API answer
(the ``result`` of the envelope), most models have a ``from_json()`` class method;
`~pgpcbot.Update.from_json` builds a whole update, its event included. A required field that is
missing or has the wrong type raises `~pgpcbot.PayloadError`; an optional one gets its default
instead.

The enums of values the server sends (`~pgpcbot.ChatKind`, `~pgpcbot.ChatMode`,
`~pgpcbot.SeatStatus`, `~pgpcbot.UpdateKind` and `~pgpcbot.BotStatus`) are ``str`` enums: a
member equals its value, so ``chat.kind == "group"`` works. A value this version does not know
does not raise: it becomes a member of its own that equals its string and matches none of the
named members.

Three models can act: `~pgpcbot.Message`, `~pgpcbot.CallbackQuery` and `~pgpcbot.SentMessage`,
with `~pgpcbot.SyncMessage`, `~pgpcbot.SyncCallbackQuery` and `~pgpcbot.SyncSentMessage` as
their twins for `~pgpcbot.SyncBot`. They hold the same data as their bases, plus methods such as
``message.reply(...)`` that go through the bot that received them.

Everything public here is also importable from ``pgpcbot``.

Example:
    >>> from pgpcbot import User
    >>> anna = User.from_json({"id": 55, "username": "anna", "someNewField": 1})
    >>> anna
    User(id=55, username='anna', first_name=None, last_name=None, is_bot=False)
    >>> anna.raw["someNewField"]
    1
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from .errors import PayloadError
from .keyboard import InlineKeyboard

if TYPE_CHECKING:
    from .bot import Bot, SyncBot

__all__ = [
    "KEEP",
    "AdminRights",
    "BotInfo",
    "BotStatus",
    "CallbackQuery",
    "CallbackQueryBase",
    "Chat",
    "ChatCard",
    "ChatKeys",
    "ChatKind",
    "ChatMemberUpdate",
    "ChatMembers",
    "ChatMode",
    "ChatPeer",
    "ChatRef",
    "Command",
    "CommandScope",
    "DeletedMessages",
    "HandleKind",
    "Limits",
    "Media",
    "Member",
    "Message",
    "MessageBase",
    "PublishedKey",
    "Resolved",
    "Seat",
    "SeatStatus",
    "SentMessage",
    "SentMessageBase",
    "SignatureStatus",
    "SyncCallbackQuery",
    "SyncMessage",
    "SyncSentMessage",
    "Update",
    "UpdateKind",
    "User",
    "UserKey",
    "Verification",
    "WebhookInfo",
    "WebhookSummary",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _Keep:
    """The default of ``markup`` in every edit, meaning "leave the buttons as they are".

    ``markup=None`` removes the buttons, and a keyboard replaces them. You rarely write ``KEEP``
    yourself: `Bot.edit_message`, `SentMessage.edit` and `CallbackQuery.edit_message` use it when
    ``markup`` is left out. It is false in a boolean test, so check for it with ``markup is KEEP``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "KEEP"

    def __bool__(self) -> bool:
        return False


KEEP: Any = _Keep()
"""The default of ``markup`` in every edit, meaning "leave the buttons as they are".

``markup=None`` removes the buttons, and a keyboard replaces them. You rarely write ``KEEP``
yourself: `Bot.edit_message`, `SentMessage.edit` and `CallbackQuery.edit_message` use it when
``markup`` is left out. It is false in a boolean test, so check for it with ``markup is KEEP``.
"""


class _OpenEnum(enum.StrEnum):
    """A StrEnum that does not blow up on a value a newer server sends.

    The unknown value becomes a pseudo member: it compares equal to its string
    and simply matches none of the named members.
    """

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if not isinstance(value, str) or not value:
            return None
        member = str.__new__(cls, value)
        member._name_ = value
        member._value_ = value
        return member


class ChatKind(_OpenEnum):
    """The kind of a chat: private, group or channel.

    A ``str`` enum, so ``chat.kind == "group"`` and ``chat.kind == ChatKind.GROUP`` are the same
    test. A kind that a newer server adds does not raise: it becomes a member of its own that
    equals its string and matches none of the names below.

    Attributes:
        PRIVATE: ``"private"``, a chat between the bot and one person.
        GROUP: ``"group"``, a group chat.
        CHANNEL: ``"channel"``, a channel. A post there may speak in the channel's name, and
            then it has no sender.

    Examples:
        >>> ChatKind.GROUP == "group"
        True
        >>> ChatKind("supergroup")  # a kind this version does not know
        <ChatKind.supergroup: 'supergroup'>
    """

    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class ChatMode(_OpenEnum):
    """Whether a chat is readable by the server or end-to-end encrypted.

    The server decides, and the mode says which body a message must carry. A private chat follows
    the bot: it is encrypted once the bot has published a key. A group or channel follows its
    history setting: readable when new members can see the history, encrypted otherwise. `Bot`
    finds out the mode before it sends, and encrypts when it must.

    Attributes:
        PROTECTED: ``"protected"``, end-to-end encrypted. Messages travel as ``armored``
            OpenPGP messages, which the server cannot read.
        OPEN: ``"open"``, readable. Messages travel as ``text``, which the server reads and
            passes on.
    """

    PROTECTED = "protected"
    OPEN = "open"


class SeatStatus(_OpenEnum):
    """Where somebody stands in a chat: the ``status`` of a `Seat`.

    In a private chat the bot's own seat is ``member``, or ``kicked`` while the person blocks it.

    Attributes:
        ADMIN: ``"admin"``, an administrator. `Seat.can` says what they may do.
        MEMBER: ``"member"``, an ordinary member.
        RESTRICTED: ``"restricted"``, a member who may do less than the others, until
            `Seat.restricted_until` when that is set.
        LEFT: ``"left"``, not in the chat, for example after leaving it.
        KICKED: ``"kicked"``, removed from the chat by somebody else. In a private chat, the
            person blocked the bot.
    """

    ADMIN = "admin"
    MEMBER = "member"
    RESTRICTED = "restricted"
    LEFT = "left"
    KICKED = "kicked"


class UpdateKind(_OpenEnum):
    """What an `Update` is about, and so which of its fields is set.

    A kind that a newer server adds becomes a member of its own. Such an update has none of those
    fields set, and its JSON is still in ``raw``.

    Attributes:
        MESSAGE: ``"message"``, a new message for the bot, in a private chat or a group. It is
            in the update's ``message``.
        EDITED_MESSAGE: ``"edited_message"``, somebody edited a message the bot had received.
            The new version is in ``message``.
        DELETED_MESSAGES: ``"deleted_messages"``, messages were deleted for everyone. They are
            listed in ``deleted``.
        CALLBACK_QUERY: ``"callback_query"``, somebody pressed a button with callback data under
            one of the bot's messages. The press is in ``callback_query``.
        MY_CHAT_MEMBER: ``"my_chat_member"``, the bot's own seat in a chat changed: added,
            promoted, restricted, removed or left; in a private chat, blocked or unblocked. The
            change is in ``chat_member``.
    """

    MESSAGE = "message"
    EDITED_MESSAGE = "edited_message"
    DELETED_MESSAGES = "deleted_messages"
    CALLBACK_QUERY = "callback_query"
    MY_CHAT_MEMBER = "my_chat_member"


class BotStatus(_OpenEnum):
    """Whether the bot is running or paused by its owner: `BotInfo.status`.

    Attributes:
        ACTIVE: ``"active"``, the bot works normally.
        PAUSED: ``"paused"``, the owner paused the bot. While it is paused, every API call
            raises `BotPausedError`, except ``GET /me``, ``GET /webhook``, ``DELETE /webhook``
            and ``POST /token/revoke``.
    """

    ACTIVE = "active"
    PAUSED = "paused"


class SignatureStatus(enum.StrEnum):
    """What the bot could tell about the OpenPGP signature of an encrypted message.

    It is the ``status`` of a `Verification`. For a message it receives, the bot checks the
    signature against the key the server lists for the sender; `BotKey.decrypt` checks it
    against the keys given in ``verify_with``. A ``str`` enum like the others, but the library
    makes it, not the server, so it only ever holds these five values.

    Attributes:
        VALID: ``"valid"``, signed with one of those keys, and the signature checks out.
        INVALID: ``"invalid"``, signed with one of those keys, but the signature does not check
            out; or the two OpenPGP engines that open every message disagree on what it says.
        UNKNOWN_KEY: ``"unknown_key"``, signed, but with none of those keys.
        UNSIGNED: ``"unsigned"``, no signature at all.
        NOT_CHECKED: ``"not_checked"``, the signature was not checked. Checking is off
            (``verify_signatures="off"``), there is no key to check against (a post in a
            channel's name has no sender; the chat's keys could not be fetched, or hold no
            usable key for the sender), or the engine that checks signatures could not read the
            message or the keys. `Verification.detail` says why, unless checking is off or no
            keys were given.
    """

    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN_KEY = "unknown_key"
    UNSIGNED = "unsigned"
    NOT_CHECKED = "not_checked"


# --- parsing helpers ---


def _obj(data: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise PayloadError(f"{where}: expected an object, got {type(data).__name__}")
    return data


def _req_int(data: Mapping[str, Any], key: str, where: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadError(f"{where}.{key}: expected an integer, got {value!r}")
    return value


def _int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _num(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _bool(data: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _when(data: Mapping[str, Any], key: str) -> datetime | None:
    ms = _int(data, key)
    if ms is None:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=ms)
    except OverflowError:
        return None


def _list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


_M = TypeVar("_M", bound="_Model")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Model:
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False, hash=False)
    """The JSON object exactly as the server sent it."""


def _rebind(cls: type[_M], obj: _Model, bot: Any) -> _M:
    values = {f.name: getattr(obj, f.name) for f in fields(obj)}
    values["_bot"] = bot
    return cls(**values)


# --- people and chats ---


@dataclass(frozen=True, slots=True, kw_only=True)
class User(_Model):
    """Somebody as a bot may know them: an ID, a handle and a name. Never a phone number.

    Attributes:
        id: The user's ID.
        username: Their handle, without ``@``, or ``None`` if they have none.
        first_name: Their first name, or ``None``.
        last_name: Their last name, or ``None``.
        is_bot: ``True`` for a bot.

    Example:
        >>> anna = User.from_json({"id": 55, "username": "anna", "firstName": "Anna"})
        >>> anna.full_name, anna.mention
        ('Anna', '@anna')
    """

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        """The first and last name, joined by a space.

        Without a name, the handle with ``@``; without either, ``"user <id>"``, as in
        ``"user 55"``.
        """
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or (f"@{self.username}" if self.username else f"user {self.id}")

    @property
    def mention(self) -> str:
        """The ``@handle`` when the user has one, `full_name` otherwise."""
        return f"@{self.username}" if self.username else self.full_name

    @classmethod
    def from_json(cls, data: Any) -> User:
        """Builds a user from the API's JSON (``id``, ``username``, ``firstName``, ...).

        Args:
            data: The user object, such as the ``from`` of a message.

        Returns:
            The user, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing or not an integer.
        """
        d = _obj(data, "user")
        return cls(
            id=_req_int(d, "id", "user"),
            username=_str(d, "username"),
            first_name=_str(d, "firstName"),
            last_name=_str(d, "lastName"),
            is_bot=_bool(d, "isBot"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatPeer(User):
    """Somebody with a key: `Chat.user`, the other side of a private chat, and `Resolved.user`.

    It has every field of `User`, and two more. ``isBot`` is sent for a lookup but not for
    the person in a private chat, where ``is_bot`` stays ``False``; ``deleted`` is the other
    way round, since a lookup never finds a closed account to begin with.

    Attributes:
        deleted: ``True`` when the person has deleted their account.
        has_key: ``True`` when the person has published an OpenPGP key. Without one, the bot
            cannot send them anything in an encrypted chat.
    """

    deleted: bool = False
    has_key: bool = False

    @classmethod
    def from_json(cls, data: Any, where: str = "chat.user") -> ChatPeer:
        """Builds the person from a ``user`` object the server sent.

        Args:
            data: The ``user`` object: of a private chat in ``GET /chats/{id}``, or of
                ``GET /resolve/{username}``.
            where: The name this object goes by in an error message.

        Returns:
            The person, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing or not an integer.
        """
        d = _obj(data, where)
        return cls(
            id=_req_int(d, "id", where),
            username=_str(d, "username"),
            first_name=_str(d, "firstName"),
            last_name=_str(d, "lastName"),
            is_bot=_bool(d, "isBot"),
            deleted=_bool(d, "deleted"),
            has_key=_bool(d, "hasKey"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatRef(_Model):
    """A chat as updates name it: its ID and kind, and the title of a group or channel.

    Every `Bot` method that takes a chat takes a ``ChatRef`` too, so ``message.chat`` can be
    passed as it is.

    Attributes:
        id: The chat's ID.
        kind: Private chat, group or channel.
        title: The name of a group or channel. ``None`` in a private chat.
        username: The public handle of a group or channel, without ``@``. ``None`` in a private
            chat, and for a group or channel that has none.
    """

    id: int
    kind: ChatKind
    title: str | None = None
    username: str | None = None

    @property
    def is_private(self) -> bool:
        """``True`` for a private chat between the bot and one person."""
        return self.kind == ChatKind.PRIVATE

    @property
    def is_group(self) -> bool:
        """``True`` for a group."""
        return self.kind == ChatKind.GROUP

    @property
    def is_channel(self) -> bool:
        """``True`` for a channel."""
        return self.kind == ChatKind.CHANNEL

    @classmethod
    def from_json(cls, data: Any) -> ChatRef:
        """Builds a chat from the API's JSON (``id``, ``kind``, ``title``, ``username``).

        Args:
            data: The chat object, such as the ``chat`` of a message.

        Returns:
            The chat, with ``data`` kept in ``raw``. A missing ``kind`` is read as ``"private"``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing or not an integer.
        """
        d = _obj(data, "chat")
        return cls(
            id=_req_int(d, "id", "chat"),
            kind=ChatKind(_str(d, "kind") or "private"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminRights(_Model):
    """What an administrator may do, one flag per right: `Seat.can`.

    A right that this version does not know is still in ``raw``, under its JSON name.

    Attributes:
        delete_messages: May delete other people's messages.
        ban_users: May ban users.
        add_members: May add members, bots included.
        pin_messages: May pin messages.
        change_info: May change the chat's info.
        manage_admins: May manage the administrators.
        invite_links: May manage invite links.
        post_messages: May post messages.
        edit_messages: May edit messages.
        manage_comments: May manage comments.
    """

    delete_messages: bool = False
    ban_users: bool = False
    add_members: bool = False
    pin_messages: bool = False
    change_info: bool = False
    manage_admins: bool = False
    invite_links: bool = False
    post_messages: bool = False
    edit_messages: bool = False
    manage_comments: bool = False

    @classmethod
    def from_json(cls, data: Any) -> AdminRights:
        """Builds the rights from the ``can`` object of an administrator's seat.

        Args:
            data: The ``can`` object, with one boolean per right (``deleteMessages``,
                ``banUsers``, ...).

        Returns:
            The rights, with ``data`` kept in ``raw``. A right that is missing is ``False``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "can")
        return cls(
            delete_messages=_bool(d, "deleteMessages"),
            ban_users=_bool(d, "banUsers"),
            add_members=_bool(d, "addMembers"),
            pin_messages=_bool(d, "pinMessages"),
            change_info=_bool(d, "changeInfo"),
            manage_admins=_bool(d, "manageAdmins"),
            invite_links=_bool(d, "inviteLinks"),
            post_messages=_bool(d, "postMessages"),
            edit_messages=_bool(d, "editMessages"),
            manage_comments=_bool(d, "manageComments"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Seat(_Model):
    """A place in a chat: the bot's own (`Chat.me`), or somebody's (`Member`).

    Which fields are set depends on `status`. An ``admin`` seat carries ``admin_rights``,
    ``can`` and maybe ``custom_title``; a ``member`` or ``restricted`` seat carries ``rights``
    and maybe ``restricted_until``; ``left`` and ``kicked`` carry nothing else.

    Attributes:
        status: Where the seat stands: admin, member, restricted, left or kicked.
        admin_rights: An administrator's rights, as a bit mask. `can` has the same rights by
            name.
        can: An administrator's rights, by name.
        custom_title: An administrator's title, or ``None``.
        rights: What a member or a restricted member may do, as a bit mask.
        restricted_until: When a restriction ends. ``None`` when it has no end, and for a seat
            that is not restricted.
    """

    status: SeatStatus
    admin_rights: int | None = None
    can: AdminRights | None = None
    custom_title: str | None = None
    rights: int | None = None
    restricted_until: datetime | None = None

    @property
    def is_admin(self) -> bool:
        """``True`` for an administrator's seat."""
        return self.status == SeatStatus.ADMIN

    @property
    def is_inside(self) -> bool:
        """``True`` when the seat is in the chat (admin, member or restricted)."""
        return self.status in (SeatStatus.ADMIN, SeatStatus.MEMBER, SeatStatus.RESTRICTED)

    @classmethod
    def from_json(cls, data: Any) -> Seat:
        """Builds a seat from the API's JSON (``status``, ``adminRights``, ``can``, ...).

        Args:
            data: The seat object, such as the ``me`` of a chat or the ``old`` and ``new`` of a
                ``my_chat_member`` update.

        Returns:
            The seat, with ``data`` kept in ``raw``. A missing ``status`` is read as ``"left"``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "seat")
        return cls(**_seat_fields(d), raw=d)


def _seat_fields(d: Mapping[str, Any]) -> dict[str, Any]:
    can = d.get("can")
    return {
        "status": SeatStatus(_str(d, "status") or "left"),
        "admin_rights": _int(d, "adminRights"),
        "can": AdminRights.from_json(can) if isinstance(can, Mapping) else None,
        "custom_title": _str(d, "customTitle"),
        "rights": _int(d, "rights"),
        "restricted_until": _when(d, "restrictedUntil"),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class Member(Seat):
    """Somebody's seat in a group or channel, with who they are.

    `Bot.get_chat_member` returns one, and `ChatMembers.members` lists them. It has every field
    of `Seat`, and one more.

    Attributes:
        user: Who holds the seat.
    """

    user: User

    @classmethod
    def from_json(cls, data: Any) -> Member:
        """Builds a member from the API's JSON: a seat with a ``user`` in it.

        Args:
            data: The member object.

        Returns:
            The member, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``user`` is missing or has no
                integer ``id``.
        """
        d = _obj(data, "member")
        return cls(user=User.from_json(d.get("user")), **_seat_fields(d), raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class Chat(ChatRef):
    """A chat the bot is in, with its mode: what `Bot.get_chat` returns (``GET /chats/{id}``).

    It has every field of `ChatRef`, and more. A private chat fills ``user`` and ``blocked``; a
    group or channel fills the others, including the bot's own seat in ``me``.

    Attributes:
        mode: Readable or end-to-end encrypted, and so which body a message must carry. If a
            server ever left it out, ``protected``: the safe guess, since a sealed message sent
            to a readable chat is refused without giving anything away.
        user: In a private chat, the person on the other side.
        blocked: In a private chat, ``True`` when the person blocked the bot.
        description: The description of a group or channel, or ``None``.
        member_count: How many members a group or channel has.
        history_visible: In a group or channel, ``True`` when new members can read the history
            from before they joined. Such a chat is readable; any other is encrypted.
        slow_mode_seconds: In a group, the slow mode delay in seconds, ``0`` when it is off.
            Sending faster than that raises `SlowModeError`.
        me: In a group or channel, the bot's own seat.
    """

    mode: ChatMode
    user: ChatPeer | None = None
    blocked: bool | None = None
    description: str | None = None
    member_count: int | None = None
    history_visible: bool | None = None
    slow_mode_seconds: int | None = None
    me: Seat | None = None

    @property
    def protected(self) -> bool:
        """``True`` when the chat is end-to-end encrypted (`mode` is ``protected``)."""
        return self.mode == ChatMode.PROTECTED

    @classmethod
    def from_json(cls, data: Any) -> Chat:
        """Builds a chat from the answer of ``GET /chats/{id}``.

        Args:
            data: The chat object.

        Returns:
            The chat, with ``data`` kept in ``raw``. A missing ``kind`` is read as
            ``"private"``, a missing ``mode`` as ``"protected"``.

        Raises:
            PayloadError: ``data`` is not an object, or it or its ``user`` has no integer
                ``id``.
        """
        d = _obj(data, "chat")
        user = d.get("user")
        me = d.get("me")
        blocked = d.get("blocked")
        history = d.get("historyVisible")
        return cls(
            id=_req_int(d, "id", "chat"),
            kind=ChatKind(_str(d, "kind") or "private"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            # missing, assume the stricter one: sealing for a readable chat is refused
            # without disclosing anything, words in clear for an encrypted one are not
            mode=ChatMode(_str(d, "mode") or "protected"),
            user=ChatPeer.from_json(user) if isinstance(user, Mapping) else None,
            blocked=blocked if isinstance(blocked, bool) else None,
            description=_str(d, "description"),
            member_count=_int(d, "memberCount"),
            history_visible=history if isinstance(history, bool) else None,
            slow_mode_seconds=_int(d, "slowModeSeconds"),
            me=Seat.from_json(me) if isinstance(me, Mapping) else None,
            raw=d,
        )


# --- names ---


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatCard(ChatRef):
    """A public group or channel seen from outside: `Resolved.chat`.

    What its short-link page shows and no more, since the bot is not in it: no ``mode`` to
    pick a body with and no seat. Pass it straight to `Bot.get_chat` once an administrator
    has added the bot.

    Attributes:
        description: The description, or ``None``.
        member_count: How many members it has.
        history_visible: ``True`` when somebody walking in can read what was said before.
    """

    description: str | None = None
    member_count: int | None = None
    history_visible: bool | None = None

    @classmethod
    def from_json(cls, data: Any) -> ChatCard:
        """Builds the card from the ``chat`` of ``GET /resolve/{username}``.

        Args:
            data: The ``chat`` object of the answer.

        Returns:
            The card, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing or not an integer.
        """
        d = _obj(data, "resolved.chat")
        history = d.get("historyVisible")
        return cls(
            id=_req_int(d, "id", "resolved.chat"),
            kind=ChatKind(_str(d, "kind") or "group"),
            title=_str(d, "title"),
            username=_str(d, "username"),
            description=_str(d, "description"),
            member_count=_int(d, "memberCount"),
            history_visible=history if isinstance(history, bool) else None,
            raw=d,
        )


class HandleKind(_OpenEnum):
    """What a handle turned out to belong to: a person, a group or a channel.

    A ``str`` enum, so ``found.kind == "user"`` and ``found.kind == HandleKind.USER`` are the
    same test. A kind a newer server adds becomes a member of its own rather than raising.

    Attributes:
        USER: ``"user"``, somebody's account - a bot's included.
        GROUP: ``"group"``, a public group.
        CHANNEL: ``"channel"``, a public channel.
    """

    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolved(_Model):
    """What a handle turned out to be: the answer of `Bot.resolve`.

    `id` is filled in whichever it was, so a caller that only wants the number never has to
    look at `kind`. Exactly one of `user` and `chat` carries the rest.

    Attributes:
        kind: Whether the handle belongs to a person, a group or a channel.
        id: The user ID, or the chat ID.
        username: The handle it actually goes by, which is not always the one that was asked
            with: somebody who answers to several returns their main one.
        user: The person, when `kind` is ``user``. ``None`` otherwise.
        chat: The group or channel, when `kind` is ``group`` or ``channel``. ``None`` otherwise.

    Example:
        >>> found = Resolved.from_json({"kind": "user", "id": 55, "username": "anna",
        ...                             "user": {"id": 55, "username": "anna"}})
        >>> found.id, found.is_user, found.chat
        (55, True, None)
    """

    kind: HandleKind
    id: int
    username: str | None = None
    user: ChatPeer | None = None
    chat: ChatCard | None = None

    @property
    def is_user(self) -> bool:
        """``True`` when the handle belongs to a person or a bot."""
        return self.kind == HandleKind.USER

    @property
    def is_chat(self) -> bool:
        """``True`` when the handle belongs to a group or a channel."""
        return self.kind in (HandleKind.GROUP, HandleKind.CHANNEL)

    @property
    def mention(self) -> str:
        """The handle with an ``@`` in front, or ``"id <n>"`` for something that has none."""
        return f"@{self.username}" if self.username else f"id {self.id}"

    @classmethod
    def from_json(cls, data: Any) -> Resolved:
        """Builds the answer of ``GET /resolve/{username}``.

        Args:
            data: The result object.

        Returns:
            What the handle was, with ``data`` kept in ``raw``. A missing ``kind`` is read as
            ``"user"``, which is what a bare ``user`` object means.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing or not an integer.
        """
        d = _obj(data, "resolved")
        user = d.get("user")
        chat = d.get("chat")
        return cls(
            kind=HandleKind(_str(d, "kind") or "user"),
            id=_req_int(d, "id", "resolved"),
            username=_str(d, "username"),
            user=ChatPeer.from_json(user, "resolved.user") if isinstance(user, Mapping) else None,
            chat=ChatCard.from_json(chat) if isinstance(chat, Mapping) else None,
            raw=d,
        )


# --- keys ---


@dataclass(frozen=True, slots=True, kw_only=True)
class UserKey(_Model):
    """Somebody's OpenPGP public key, as the server hands it out.

    `Bot.get_user_key` returns one, and `ChatKeys.keys` lists them. The fingerprint is what the
    server *says*: `PublicKey.from_user_key` reads the key and checks that it has that
    fingerprint before anything is sealed to it.

    Attributes:
        user_id: Whose key it is.
        fingerprint: The key's fingerprint as the server gives it, or ``None``.
        public_key: The key, as ASCII-armored text.

    Warning:
        The keys come from the server. A server that lies could hand out a key of its own in
        place of a person's. ``Bot(pin_keys=True)`` notices when a person's key changes.
    """

    user_id: int
    fingerprint: str | None
    public_key: str

    @classmethod
    def from_json(cls, data: Any) -> UserKey:
        """Builds a key from the API's JSON (``userId``, ``fingerprint``, ``publicKey``).

        Args:
            data: The key object, such as the answer of ``GET /users/{userId}/key``.

        Returns:
            The key, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, its ``publicKey`` is missing, or its
                ``userId`` is missing or not an integer.
        """
        d = _obj(data, "key")
        public_key = _str(d, "publicKey")
        if public_key is None:
            raise PayloadError("key.publicKey: missing")
        return cls(
            user_id=_req_int(d, "userId", "key"), fingerprint=_str(d, "fingerprint"), public_key=public_key, raw=d
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatKeys(_Model):
    """The keys a message into an encrypted chat must be sealed to: `Bot.get_chat_keys`.

    The answer of ``GET /chats/{id}/keys``. `Bot` fetches these keys by itself before it sends
    into an encrypted chat. The bot's own key is never in the list: add it yourself when you seal
    by hand (`BotKey.seal` does). Bots in privacy mode are left out, as the app does.

    Attributes:
        keys: One `UserKey` per member who has a key. In a private chat, the person's only.
        member_count: In a group or channel, how many members it has. Always more than the
            number of keys: the bot itself is not in ``keys``, nor is a member without a key.
            ``None`` in a private chat.
    """

    keys: tuple[UserKey, ...]
    member_count: int | None = None

    @classmethod
    def from_json(cls, data: Any) -> ChatKeys:
        """Builds the list from the answer of ``GET /chats/{id}/keys``.

        Args:
            data: The answer, with ``keys`` and, in a group or channel, ``memberCount``.

        Returns:
            The keys, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its keys is not a valid key
                object (see `UserKey.from_json`).
        """
        d = _obj(data, "keys")
        return cls(
            keys=tuple(UserKey.from_json(k) for k in _list(d, "keys")),
            member_count=_int(d, "memberCount"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMembers(_Model):
    """The members of a group or channel, as the bot may see them: `Bot.get_chat_members`.

    The answer of ``GET /chats/{id}/members``. The bot sees what a member in its seat would see:
    where the member list is hidden, a bot that is not an admin sees the admins only.

    Attributes:
        members: Each member, with their seat.
        member_count: How many members are in ``members``. It is the length of the list the bot
            may see, not the size of the chat: `Chat.member_count` counts everyone.
    """

    members: tuple[Member, ...]
    member_count: int

    @classmethod
    def from_json(cls, data: Any) -> ChatMembers:
        """Builds the list from the answer of ``GET /chats/{id}/members``.

        Args:
            data: The answer, with ``members`` and ``memberCount``.

        Returns:
            The members, with ``data`` kept in ``raw``. Without ``memberCount``,
            ``member_count`` is the length of ``members``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its members is not a valid
                member object (see `Member.from_json`).
        """
        d = _obj(data, "members")
        members = tuple(Member.from_json(m) for m in _list(d, "members"))
        count = _int(d, "memberCount")
        return cls(members=members, member_count=count if count is not None else len(members), raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishedKey(_Model):
    """What the server answers when the bot publishes its key (``PUT /key``).

    `Bot.publish_key` and `Bot.rotate_key` return it. From then on the bot is protected: its
    private chats are end-to-end encrypted, and only its owner can turn that off, from the app.

    Attributes:
        fingerprint: The fingerprint of the key now published.
        mode: The bot's mode now, ``protected``.
    """

    fingerprint: str
    mode: ChatMode

    @classmethod
    def from_json(cls, data: Any) -> PublishedKey:
        """Builds the answer of ``PUT /key`` from its JSON.

        Args:
            data: The answer, with ``fingerprint`` and ``mode``.

        Returns:
            The published key, with ``data`` kept in ``raw``. A missing ``fingerprint`` is
            ``""``, a missing ``mode`` is ``"protected"``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "key")
        return cls(fingerprint=_str(d, "fingerprint") or "", mode=ChatMode(_str(d, "mode") or "protected"), raw=d)


# --- the bot ---


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits(_Model):
    """The numbers the server you are on enforces: `BotInfo.limits`.

    Read them rather than hard-coding them. `Bot` uses some by itself: ``text_max`` to check a
    text before sending it, and the four rate limits to pace its own `RateLimiter`.

    Each field is a JSON name of ``limits`` in Python style: ``text_max`` is ``textMax``,
    ``poll_timeout_max_s`` is ``pollTimeoutMaxS``. The defaults are the documented numbers. They
    apply only when a server leaves a name out, or sends something that is not a positive whole
    number. A limit this version has no field for is still in ``raw``: read it with `get`.

    Attributes:
        text_max: The longest ``text``, in UTF-16 units, as JavaScript counts them: most emoji
            count 2. Default 4096.
        armored_max: The longest ``armored`` body, in characters. Default 131072.
        markup_bytes: The largest keyboard, in bytes of JSON. Default 8192.
        keyboard_rows: The most rows of buttons under one message. Default 20.
        buttons_per_row: The most buttons in one row. Default 8.
        buttons_total: The most buttons under one message. Default 100.
        button_text_max: The longest button text, in UTF-16 units. Default 64.
        callback_data_bytes: The longest ``callback_data`` of a button, in bytes of UTF-8.
            Default 64.
        url_max: The longest URL of a link button, a callback answer or a webhook, in
            characters. Default 1024.
        callback_answer_max: The longest text of a callback answer, in characters. Default 200.
        callback_window_ms: How long a button press can still be answered, in milliseconds.
            Default 60000.
        poll_timeout_max_s: The longest wait of a long poll, in seconds. Default 50.
        poll_limit_max: The most updates one poll hands over. Default 100.
        webhook_timeout_ms: How long the server waits for the webhook to answer, in
            milliseconds. Default 10000.
        sends_per_second: Messages per second, over all chats. Default 30.
        sends_per_group_per_minute: Messages per minute into one group or channel. Default 20.
        sends_per_private_per_second: Messages per second into one private chat. Default 1.
        api_calls_per_minute: API calls per minute, of any kind. Default 600.
        commands_per_scope: The most commands in one menu. Default 100.
        command_description_max: The longest command description, in characters. Default 256.
    """

    text_max: int = 4096
    armored_max: int = 131072
    markup_bytes: int = 8192
    keyboard_rows: int = 20
    buttons_per_row: int = 8
    buttons_total: int = 100
    button_text_max: int = 64
    callback_data_bytes: int = 64
    url_max: int = 1024
    callback_answer_max: int = 200
    callback_window_ms: int = 60000
    poll_timeout_max_s: int = 50
    poll_limit_max: int = 100
    webhook_timeout_ms: int = 10000
    sends_per_second: int = 30
    sends_per_group_per_minute: int = 20
    sends_per_private_per_second: int = 1
    api_calls_per_minute: int = 600
    commands_per_scope: int = 100
    command_description_max: int = 256

    def get(self, name: str, default: int | None = None) -> int | None:
        """Reads any limit by its JSON name, including the ones this version has no field for.

        It reads what the server sent, in ``raw``. A name the server left out gives ``default``,
        even when a field has a documented default for it.

        Args:
            name: The JSON name, such as ``"botsPerChat"``.
            default: What to return when the server did not send that limit as an integer.

        Returns:
            The server's number, or ``default``.

        Example:
            >>> limits = Limits.from_json({"textMax": 4096, "botsPerChat": 20})
            >>> limits.get("botsPerChat")
            20
            >>> limits.get("pollLimitMax") is None
            True
            >>> limits.poll_limit_max
            100
        """
        value = self.raw.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    @classmethod
    def from_json(cls, data: Any) -> Limits:
        """Builds the limits from the ``limits`` object of ``GET /me``.

        Args:
            data: The ``limits`` object. Anything else, ``None`` included, gives the defaults.

        Returns:
            The limits. The object itself is kept in ``raw``.
        """
        d = data if isinstance(data, Mapping) else {}
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "raw":
                continue
            # text_max -> textMax, poll_timeout_max_s -> pollTimeoutMaxS
            head, *rest = f.name.split("_")
            value = d.get(head + "".join(p.capitalize() for p in rest))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values[f.name] = value
        return cls(**values, raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookSummary(_Model):
    """The webhook in brief, as part of `BotInfo`.

    `Bot.get_webhook` gives the whole picture, as a `WebhookInfo`.

    Attributes:
        url: The address updates are posted to, or ``None`` when the bot uses long polling.
        has_secret: ``True`` when a secret is set. The secret itself is never shown.
        failures: Failed deliveries in a row. After ten, the owner is told in the app.
        last_error: What went wrong last, or ``None``.
    """

    url: str | None = None
    has_secret: bool = False
    failures: int = 0
    last_error: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> WebhookSummary:
        """Builds the summary from the ``webhook`` object of ``GET /me``.

        Args:
            data: The ``webhook`` object. Anything else, ``None`` included, gives the defaults.

        Returns:
            The summary. The object itself is kept in ``raw``.
        """
        d = data if isinstance(data, Mapping) else {}
        return cls(
            url=_str(d, "url"),
            has_secret=_bool(d, "hasSecret"),
            failures=_int(d, "failures") or 0,
            last_error=_str(d, "lastError"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BotInfo(_Model):
    """The bot as the server holds it, and the limits the server enforces (``GET /me``).

    `Bot.get_me` returns it, and `Bot` keeps the answer it got at start-up in `Bot.me`. It works
    while the bot is paused, so it is the way to find out why nothing else does.

    Attributes:
        id: The bot's user ID.
        username: Its handle, without ``@``. It ends in ``bot``.
        name: Its display name.
        about: The one line under the name on its profile, or ``None``.
        description: What the bot does, shown before the first message, or ``None``.
        mode: The mode of the bot's private chats: ``protected`` once it has published a key,
            ``open`` before.
        fingerprint: The fingerprint of the published key, or ``None``.
        privacy_mode: ``True`` when, in groups, the bot gets only what is addressed to it:
            commands, mentions of its handle and replies to its messages. ``False`` when it
            gets every message of a readable group. The owner sets it in the app.
        allow_groups: ``True`` when people can add the bot to groups.
        status: ``active``, or ``paused`` when the owner paused the bot.
        webhook: The webhook in brief.
        queued: How many updates are waiting to be fetched or delivered.
        limits: The numbers this server enforces.

    Example:
        Read a limit instead of hard-coding it::

            me = await bot.get_me()
            if len(report) > me.limits.text_max:
                report = report[: me.limits.text_max - 3] + "..."
    """

    id: int
    username: str
    name: str
    about: str | None = None
    description: str | None = None
    mode: ChatMode = ChatMode.OPEN
    fingerprint: str | None = None
    privacy_mode: bool = True
    allow_groups: bool = True
    status: BotStatus = BotStatus.ACTIVE
    webhook: WebhookSummary = field(default_factory=WebhookSummary)
    queued: int = 0
    limits: Limits = field(default_factory=Limits)

    @property
    def protected(self) -> bool:
        """``True`` when `mode` is ``protected``, so the bot's private chats are encrypted."""
        return self.mode == ChatMode.PROTECTED

    @property
    def paused(self) -> bool:
        """``True`` when the owner paused the bot.

        While it is paused, most API calls raise `BotPausedError` (see `BotStatus.PAUSED`).
        """
        return self.status == BotStatus.PAUSED

    @classmethod
    def from_json(cls, data: Any) -> BotInfo:
        """Builds the bot from the answer of ``GET /me``.

        Args:
            data: The answer.

        Returns:
            The bot, with ``data`` kept in ``raw``. A missing ``mode`` is read as
            ``"protected"`` when ``fingerprint`` is set, ``"open"`` otherwise.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing or not an integer.
        """
        d = _obj(data, "me")
        return cls(
            id=_req_int(d, "id", "me"),
            username=_str(d, "username") or "",
            name=_str(d, "name") or "",
            about=_str(d, "about"),
            description=_str(d, "description"),
            # a bot is protected exactly when it has a published key
            mode=ChatMode(_str(d, "mode") or ("protected" if _str(d, "fingerprint") else "open")),
            fingerprint=_str(d, "fingerprint"),
            privacy_mode=_bool(d, "privacyMode", True),
            allow_groups=_bool(d, "allowGroups", True),
            status=BotStatus(_str(d, "status") or "active"),
            webhook=WebhookSummary.from_json(d.get("webhook")),
            queued=_int(d, "queued") or 0,
            limits=Limits.from_json(d.get("limits")),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookInfo(_Model):
    """The webhook, if any, and how deliveries are going: `Bot.get_webhook`.

    ``GET /webhook`` fills every field. ``POST /webhook`` and ``DELETE /webhook``
    (`Bot.set_webhook` and `Bot.delete_webhook`) answer with ``url``, ``has_secret`` and
    ``pending`` only, and the other fields keep their defaults.

    Attributes:
        url: The address updates are posted to, or ``None`` when the bot uses long polling.
        has_secret: ``True`` when a secret is set. The secret itself is never shown.
        pending: How many updates are still waiting.
        failures: Failed deliveries in a row. After ten, the owner is told in the app.
        last_error: What went wrong last, or ``None``.
        last_ok_at: When a delivery last worked, or ``None``.
        next_attempt_at: While the server backs off after a failure, when it tries again;
            otherwise ``None``.

    Example:
        See how the webhook is doing::

            info = await bot.get_webhook()
            if info.failures:
                print(f"{info.failures} failures in a row, last: {info.last_error}")
    """

    url: str | None = None
    has_secret: bool = False
    pending: int = 0
    failures: int = 0
    last_error: str | None = None
    last_ok_at: datetime | None = None
    next_attempt_at: datetime | None = None

    @classmethod
    def from_json(cls, data: Any) -> WebhookInfo:
        """Builds the webhook's state from the answer of ``GET /webhook``.

        ``POST /webhook`` and ``DELETE /webhook`` answer with the same object, with fewer fields.

        Args:
            data: The answer.

        Returns:
            The webhook's state, with ``data`` kept in ``raw``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "webhook")
        return cls(
            url=_str(d, "url"),
            has_secret=_bool(d, "hasSecret"),
            pending=_int(d, "pending") or 0,
            failures=_int(d, "failures") or 0,
            last_error=_str(d, "lastError"),
            last_ok_at=_when(d, "lastOkAt"),
            next_attempt_at=_when(d, "nextAttemptAt"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Command(_Model):
    """One entry of the menu the app shows when somebody types ``/``.

    `Bot.get_commands`, given a scope, returns a list of them. You can also make one to pass to
    `Bot.set_commands`, which takes ``(name, description)`` pairs as well.

    Attributes:
        command: The name, without the ``/``: 1 to 32 lowercase letters, digits and ``_``.
        description: What the command does, 1 to 256 characters.
    """

    command: str
    description: str

    def to_json(self) -> dict[str, str]:
        """Turns the command into the API's JSON form.

        Returns:
            A dict with ``command`` and ``description``, as ``PUT /commands`` takes it.

        Example:
            >>> Command(command="start", description="Say hello").to_json()
            {'command': 'start', 'description': 'Say hello'}
        """
        return {"command": self.command, "description": self.description}

    @classmethod
    def from_json(cls, data: Any) -> Command:
        """Builds a command from the API's JSON (``command``, ``description``).

        Args:
            data: One command object, as ``GET /commands`` lists them.

        Returns:
            The command, with ``data`` kept in ``raw``. A missing field is ``""``.

        Raises:
            PayloadError: ``data`` is not an object.
        """
        d = _obj(data, "command")
        return cls(command=_str(d, "command") or "", description=_str(d, "description") or "", raw=d)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandScope(_Model):
    """One command menu, and where the app shows it.

    `Bot.get_commands`, without a scope, lists one per scope; `Bot.set_commands` and
    `Bot.delete_commands` answer with the one they changed. The app shows the narrowest scope
    that has commands: in a private chat ``private``, then ``default``; in a group
    ``chat:<id>``, then ``groupAdmins`` (for admins), then ``groups``, then ``default``.

    Attributes:
        scope: ``"default"``, ``"private"``, ``"groups"``, ``"groupAdmins"``, or
            ``"chat:<id>"`` for one chat.
        commands: The commands of the menu.
    """

    scope: str
    commands: tuple[Command, ...]

    @classmethod
    def from_json(cls, data: Any) -> CommandScope:
        """Builds a menu from the API's JSON (``scope``, ``commands``).

        Args:
            data: One scope object, as ``GET /commands`` lists them and ``PUT /commands``
                answers.

        Returns:
            The menu, with ``data`` kept in ``raw``. A missing ``scope`` is ``"default"``.

        Raises:
            PayloadError: ``data`` is not an object, or one of its commands is not an object.
        """
        d = _obj(data, "scope")
        return cls(
            scope=_str(d, "scope") or "default",
            commands=tuple(Command.from_json(c) for c in _list(d, "commands")),
            raw=d,
        )


# --- messages ---


@dataclass(frozen=True, slots=True, kw_only=True)
class Media(_Model):
    """A picture, clip or file attached to a message: one item of ``message.media``.

    In a readable chat the server lists only the pictures and videos that were sent in the clear.
    In an encrypted chat the list comes from inside the encrypted message, so it describes every
    kind of attachment, and the files on the server are normally encrypted too.
    `Bot.download_media` downloads a file and decrypts it. That always works in a private chat. In
    an encrypted group the sender's phone seals the file only to the members' keys, so it works
    only when the bot is among them (its privacy mode is off); otherwise the download raises
    `NotARecipientError`.

    Attributes:
        id: The file's ID, a UUID.
        kind: What it is: ``"image"``, ``"video"``, ``"audio"`` or ``"file"``.
        mime: The MIME type, such as ``"image/jpeg"``, or ``None``.
        url: Where the file is, ``/media/<id>`` on the API server. `Bot.download_media` uses it.
        width: The width in pixels, or ``None``.
        height: The height in pixels, or ``None``.
        duration_ms: The length of a clip, in milliseconds, or ``None``. The API calls it
            ``duration``.
        name: The file name, when the sender's app gives one, or ``None``. Only in encrypted
            chats.
        size: The size in bytes, when the sender's app gives it, or ``None``. Only in encrypted
            chats.
        encrypted: ``True`` when the file on the server is encrypted with OpenPGP.
            `Bot.download_media` then decrypts it.
    """

    id: str
    kind: str
    mime: str | None = None
    url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    name: str | None = None
    size: int | None = None
    encrypted: bool = False

    @classmethod
    def from_json(cls, data: Any) -> Media:
        """Builds an attachment from an item of the ``media`` of a message in a readable chat.

        The server sends no ``name``, ``size`` or ``encrypted`` there, so they keep their
        defaults. Attachments of encrypted chats are read from inside the decrypted message
        instead.

        Args:
            data: The media object (``id``, ``kind``, ``mime``, ``url``, ``width``,
                ``height``, ``duration``).

        Returns:
            The attachment, with ``data`` kept in ``raw``. A missing ``kind`` is ``"file"``.

        Raises:
            PayloadError: ``data`` is not an object, or its ``id`` is missing.
        """
        d = _obj(data, "media")
        media_id = _str(d, "id")
        if not media_id:
            raise PayloadError("media.id: missing")
        duration = _num(d, "duration")
        return cls(
            id=media_id,
            kind=_str(d, "kind") or "file",
            mime=_str(d, "mime"),
            url=_str(d, "url"),
            width=_int(d, "width"),
            height=_int(d, "height"),
            duration_ms=round(duration) if duration is not None else None,
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class Verification:
    """The outcome of checking the signature of an encrypted message.

    The bot sets one on every message it decrypts (``message.verification``; ``None`` in a
    readable chat), and `BotKey.decrypt` returns one in its `Decrypted`. By default
    (``verify_signatures="auto"``) the bot only reports what it found: a message with a bad
    signature, or none, still reaches the handlers. With ``verify_signatures="require"``, a
    message that is not ``valid`` raises `SignatureError` and goes to the error handlers instead.

    Attributes:
        status: What the check found.
        signer_fingerprint: When `status` is ``valid``, the fingerprint of the key that made the
            signature, in upper-case hex. ``None`` otherwise.
        detail: Why the status is not ``valid``, in a few words for a log, or ``None``. It is
            written for people: branch on `status`, not on this text.

    Example:
        Tell the sender what the bot found::

            from pgpcbot import filters as f

            @bot.on_message(f.encrypted)
            async def check(message):
                v = message.verification
                if v.valid:
                    await message.reply("Signed with your key.")
                else:
                    await message.reply(f"Signature: {v.status} ({v.detail})")

    Note:
        A ``valid`` signature proves that the message comes from whoever holds the key the
        server lists for the sender, and that nobody changed it. It does not prove that the key
        belongs to that person: the server hands out the keys.
    """

    status: SignatureStatus
    signer_fingerprint: str | None = None
    detail: str | None = None

    @property
    def valid(self) -> bool:
        """``True`` when `status` is ``valid``."""
        return self.status == SignatureStatus.VALID


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageBase(_Model):
    """A message a person wrote, as the bot receives it: the data, without methods.

    Handlers get it as a `Message` (a `SyncMessage` on a `SyncBot`), which adds `Message.reply`
    and the other methods. The bot's own messages never arrive as updates: sends and edits
    return them as `SentMessage` objects.

    In an end-to-end encrypted chat, the bot decrypts the message before any handler sees it.
    `text` and `media` then come from inside it, `armored` keeps the original, `plaintext` the
    exact decrypted body, and `verification` says what the signature check found. A message the
    bot cannot decrypt goes to the error handlers instead. In a readable chat the server reads
    the message itself: `armored`, `plaintext` and `verification` are ``None``.

    Attributes:
        id: The message's ID, unique inside its chat.
        chat: The chat it was written in.
        from_user: Who wrote it. ``None`` for a channel post in the channel's name: the bot,
            like any reader, is not told who wrote it.
        date: When it was sent, in UTC.
        reply_to: The ID of the message it answers, or ``None``.
        kind: The kind of message, as the server says: ``"user"`` for anything a person typed
            or attached.
        text: What the person wrote, or the caption of a picture. ``None`` when there are no
            words, as for a sticker, a call notice or a payment request. In a readable chat
            the server hands it over trimmed, with runs of empty lines folded into one, and
            cut at 4000 characters. In an encrypted chat it is the decrypted text as the
            person wrote it; only ``CR LF`` line ends become ``LF``.
        media: The pictures, clips and files that came with it, one `Media` each. Empty when
            there are none.
        signature: For a channel post in the channel's name, the name a signed post shows its
            readers. ``None`` when the channel does not sign its posts, and for any other
            message.
        armored: In an encrypted chat, the OpenPGP message as it arrived, ASCII-armored.
            ``None`` in a readable chat.
        plaintext: In an encrypted chat, the decrypted body exactly as the sender's app wrote
            it, in the app's internal format: a picture or a sticker is a marker followed by
            JSON. The bot reads `text` and `media` out of it. ``None`` in a readable chat.
        verification: In an encrypted chat, what the check of the sender's signature found.
            ``None`` in a readable chat.

    Example:
        >>> message = MessageBase.from_json({
        ...     "id": 1834,
        ...     "chat": {"id": 7781, "kind": "private"},
        ...     "from": {"id": 55, "username": "anna"},
        ...     "date": 1767225600000,
        ...     "text": "/weather rome",
        ... })
        >>> message.chat_id, message.from_user.mention, message.is_command
        (7781, '@anna', True)
        >>> message.date.isoformat()
        '2026-01-01T00:00:00+00:00'

    Note:
        In a readable chat, the server marks a message ``armored`` whenever its text holds an
        OpenPGP armor line, as when somebody pastes a PGP block. If the bot cannot decrypt it,
        the handlers get it as words anyone in the chat can read: `text` holds the block, and
        `armored` is ``None``.
    """

    id: int
    chat: ChatRef
    from_user: User | None
    date: datetime
    reply_to: int | None = None
    kind: str = "user"
    text: str | None = None
    media: tuple[Media, ...] = ()
    signature: str | None = None
    armored: str | None = None
    plaintext: str | None = None
    verification: Verification | None = None
    _bot: Any = field(default=None, repr=False, compare=False, hash=False)

    @property
    def encrypted(self) -> bool:
        """``True`` when the message came end-to-end encrypted, so `armored` is set."""
        return self.armored is not None

    @property
    def chat_id(self) -> int:
        """The ID of the chat, the same as ``message.chat.id``."""
        return self.chat.id

    @property
    def is_command(self) -> bool:
        """``True`` when `text` starts with ``/``.

        It looks at the first character only. `~pgpcbot.filters.parse_command` tells a real
        command from a text such as ``"/ hi"``, and reads its name and arguments.
        """
        return bool(self.text) and self.text.startswith("/")  # type: ignore[union-attr]

    @classmethod
    def from_json(cls, data: Any) -> MessageBase:
        """Builds a message from the API's JSON, as an update carries it.

        Nothing is decrypted here: from an encrypted chat, ``text`` stays ``None`` and
        ``armored`` holds the message. The bot decrypts messages before its handlers get them.

        Args:
            data: The ``message`` object of an update.

        Returns:
            The message, with ``data`` kept in ``raw``. A missing ``kind`` is ``"user"``.
            Called on `Message` or `SyncMessage`, it builds one of those, attached to no bot
            (see `Message.bind`).

        Raises:
            PayloadError: ``data`` is not an object; its ``id`` or ``date`` is missing or not
                an integer; its ``chat`` is missing or not valid; or its ``from`` or one of
                its ``media`` items is not valid.
        """
        d = _obj(data, "message")
        date = _when(d, "date")
        if date is None:
            raise PayloadError("message.date: expected unix milliseconds")
        sender = d.get("from")
        return cls(
            id=_req_int(d, "id", "message"),
            chat=ChatRef.from_json(d.get("chat")),
            from_user=User.from_json(sender) if isinstance(sender, Mapping) else None,
            date=date,
            reply_to=_int(d, "replyTo"),
            kind=_str(d, "kind") or "user",
            text=_str(d, "text"),
            media=tuple(Media.from_json(m) for m in _list(d, "media") if isinstance(m, Mapping)),
            signature=_str(d, "signature"),
            armored=_str(d, "armored"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SentMessageBase(_Model):
    """A message of the bot's own, as a send or an edit returns it: the data, without methods.

    `Bot.send_message`, `Bot.edit_message` and the methods that send, such as `Message.reply`,
    return it as a `SentMessage` (a `SyncSentMessage` on a `SyncBot`), which adds
    `SentMessage.edit` and `SentMessage.delete`. The fields are what the server stored.

    Attributes:
        id: The message's ID, unique inside its chat.
        chat: The chat it was sent to.
        date: When it was sent, in UTC.
        reply_to: The ID of the message it quotes, or ``None``.
        markup: Its buttons, or ``None`` when it has none.
        text: Its text, in a readable chat. ``None`` in an encrypted chat, where the server
            cannot read it.
        armored: In an encrypted chat, the OpenPGP message that was sent, sealed and signed,
            ASCII-armored. ``None`` in a readable chat.
    """

    id: int
    chat: ChatRef
    date: datetime
    reply_to: int | None = None
    markup: InlineKeyboard | None = None
    text: str | None = None
    armored: str | None = None
    _bot: Any = field(default=None, repr=False, compare=False, hash=False)

    @property
    def encrypted(self) -> bool:
        """``True`` when the message went out end-to-end encrypted, so `armored` is set."""
        return self.armored is not None

    @classmethod
    def from_json(cls, data: Any) -> SentMessageBase:
        """Builds the message from the answer of ``POST /messages`` or ``PATCH /messages/{id}``.

        Args:
            data: The answer.

        Returns:
            The message, with ``data`` kept in ``raw``. Called on `SentMessage` or
            `SyncSentMessage`, it builds one of those, attached to no bot (see
            `SentMessage.bind`).

        Raises:
            PayloadError: ``data`` is not an object; its ``id`` or ``date`` is missing or not
                an integer; or its ``chat`` is missing or not valid.
        """
        d = _obj(data, "sent message")
        date = _when(d, "date")
        if date is None:
            raise PayloadError("message.date: expected unix milliseconds")
        markup = d.get("markup")
        return cls(
            id=_req_int(d, "id", "message"),
            chat=ChatRef.from_json(d.get("chat")),
            date=date,
            reply_to=_int(d, "replyTo"),
            markup=InlineKeyboard.from_json(markup) if isinstance(markup, Mapping) else None,
            text=_str(d, "text"),
            armored=_str(d, "armored"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CallbackQueryBase(_Model):
    """A press on a button with callback data: the data, without methods.

    Handlers get it as a `CallbackQuery` (a `SyncCallbackQuery` on a `SyncBot`), which adds
    `CallbackQuery.answer` and the methods that change the message. The pressed button spins on
    the person's phone until the bot answers, which it can do once, within 60 seconds of the
    press. A press that nothing answered gets an empty answer from the bot once the update is
    handled, so the button stops spinning (unless ``auto_answer_callbacks=False``).

    Attributes:
        id: The ID of the press, to answer it with.
        from_user: Who pressed the button.
        chat: The chat of the message the button is under.
        message_id: The ID of the message the button is under, one of the bot's own.
        data: The ``callback_data`` of the button that was pressed.
        expires_at: The deadline to answer, 60 seconds after the press, in UTC. ``None`` when
            the server does not say.

    Warning:
        The data of a button travels in clear, even in an encrypted chat: the server has to know
        which button was pressed. Put in it an ID that means something only to your bot
        (``order:8812``), not personal data.
    """

    id: str
    from_user: User
    chat: ChatRef
    message_id: int
    data: str
    expires_at: datetime | None = None
    _bot: Any = field(default=None, repr=False, compare=False, hash=False)

    @property
    def expired(self) -> bool:
        """``True`` once `expires_at` has passed, by this computer's clock.

        After that the server refuses an answer with `CallbackExpiredError`. ``False`` when
        `expires_at` is ``None``.
        """
        return self.expires_at is not None and datetime.now(UTC) >= self.expires_at

    @classmethod
    def from_json(cls, data: Any) -> CallbackQueryBase:
        """Builds a press from the ``callback`` object of a ``callback_query`` update.

        Args:
            data: The ``callback`` object.

        Returns:
            The press, with ``data`` kept in ``raw``. When the object has no ``data`` string,
            the `data` field is ``""``. Called on `CallbackQuery` or `SyncCallbackQuery`, it
            builds one of those, attached to no bot (see `CallbackQuery.bind`).

        Raises:
            PayloadError: ``data`` is not an object; its ``id`` is missing or not a string; its
                ``messageId`` is missing or not an integer; or its ``from`` or ``chat`` is
                missing or not valid.
        """
        d = _obj(data, "callback")
        query_id = _str(d, "id")
        if not query_id:
            raise PayloadError("callback.id: missing")
        return cls(
            id=query_id,
            from_user=User.from_json(d.get("from")),
            chat=ChatRef.from_json(d.get("chat")),
            message_id=_req_int(d, "messageId", "callback"),
            data=_str(d, "data") or "",
            expires_at=_when(d, "expiresAt"),
            raw=d,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletedMessages(_Model):
    """Messages deleted for everyone in one chat: what a ``deleted_messages`` update carries.

    Handlers registered with `Router.on_deleted_messages` get it. In a group it lists the
    messages the bot had received. In a private chat it lists any message of the chat, the
    bot's own included: also the ones the bot deleted itself, and a whole history the person
    cleared. It does not carry who deleted them. Its ``raw`` is the JSON of the whole update.

    Attributes:
        chat: The chat the messages were deleted from.
        message_ids: The IDs of the deleted messages.

    Example:
        Forget what the bot kept about them::

            @bot.on_deleted_messages()
            async def forget(deleted):
                for message_id in deleted.message_ids:
                    await db.forget(deleted.chat.id, message_id)
    """

    chat: ChatRef
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMemberUpdate(_Model):
    """A change of the bot's own seat in a chat: what a ``my_chat_member`` update carries.

    Handlers registered with `Router.on_chat_member` get it. The bot was added, promoted,
    restricted, removed, or it left; in a private chat, the person blocked it (the seat becomes
    ``kicked``) or unblocked it (``member`` again). `joined`, `left`, `blocked` and `unblocked`
    name the usual cases; compare `old` and `new` for the others, such as a promotion
    (``change.new.is_admin``). Its ``raw`` is the JSON of the whole update.

    Attributes:
        chat: The chat where the bot's seat changed.
        from_user: Who made the change, or ``None`` when the update does not say.
        date: When it changed, in UTC, or ``None`` when the update does not say.
        old: The bot's seat before the change.
        new: The bot's seat after it.

    Example:
        Say hello in a new group, and log who blocks the bot::

            @bot.on_chat_member()
            async def seat_changed(change):
                if change.joined and change.chat.is_group:
                    await bot.send_message(change.chat, "Hello. Type /help to see what I do.")
                elif change.blocked:
                    log.info("blocked in chat %s", change.chat.id)
    """

    chat: ChatRef
    from_user: User | None
    date: datetime | None
    old: Seat
    new: Seat

    @property
    def joined(self) -> bool:
        """``True`` when the bot is in the chat now and was not before.

        It was added to the chat, or unblocked in a private chat. In the chat means an
        ``admin``, ``member`` or ``restricted`` seat (`Seat.is_inside`).
        """
        return self.new.is_inside and not self.old.is_inside

    @property
    def left(self) -> bool:
        """``True`` when the bot was in the chat and is not any more.

        It was removed or it left, or the person blocked it in a private chat.
        """
        return self.old.is_inside and not self.new.is_inside

    @property
    def blocked(self) -> bool:
        """``True`` when the person in a private chat blocked the bot.

        Until they unblock it, a send to them raises `BlockedByUserError`.
        """
        return self.chat.is_private and self.new.status == SeatStatus.KICKED

    @property
    def unblocked(self) -> bool:
        """``True`` when the person in a private chat unblocked the bot."""
        return self.chat.is_private and self.old.status == SeatStatus.KICKED and self.new.is_inside


@dataclass(frozen=True, slots=True, kw_only=True)
class Update(_Model):
    """One thing that happened, as the server delivers it.

    `kind` says what happened, and so which of the other fields is set: `message` for a new or
    an edited message, `callback_query` for a button press, `deleted` for messages deleted for
    everyone, `chat_member` for a change of the bot's own seat. The others are ``None``.

    Handlers get the event, not the update: a `Message`, a `CallbackQuery`, a `DeletedMessages`
    or a `ChatMemberUpdate`. A handler registered with `Router.on_update`, and any handler with
    an ``update`` parameter, gets the `Update` itself. Its events are data only: a `MessageBase`
    or a `CallbackQueryBase`, without the methods. A kind that a newer server adds still parses:
    such an update has no event, and its JSON is in ``raw``.

    Attributes:
        update_id: The update's ID. It grows by one with each update the bot gets.
        kind: What happened.
        message: For a ``message`` update, the new message; for an ``edited_message`` update,
            the message as it is after the edit.
        callback_query: For a ``callback_query`` update, the button press.
        deleted: For a ``deleted_messages`` update, the messages that were deleted.
        chat_member: For a ``my_chat_member`` update, the change of the bot's seat.

    Example:
        >>> update = Update.from_json({
        ...     "updateId": 412,
        ...     "kind": "deleted_messages",
        ...     "chat": {"id": 7781, "kind": "private"},
        ...     "messageIds": [1834, 1835],
        ... })
        >>> update.kind
        <UpdateKind.DELETED_MESSAGES: 'deleted_messages'>
        >>> update.deleted.message_ids, update.chat.id, update.message
        ((1834, 1835), 7781, None)
    """

    update_id: int
    kind: UpdateKind
    message: MessageBase | None = None
    callback_query: CallbackQueryBase | None = None
    deleted: DeletedMessages | None = None
    chat_member: ChatMemberUpdate | None = None

    @property
    def chat(self) -> ChatRef | None:
        """The chat the update is about, whatever its kind.

        ``None`` only for a kind this version does not know.
        """
        if self.message is not None:
            return self.message.chat
        if self.callback_query is not None:
            return self.callback_query.chat
        if self.deleted is not None:
            return self.deleted.chat
        if self.chat_member is not None:
            return self.chat_member.chat
        return None

    @property
    def from_user(self) -> User | None:
        """Who caused the update, when it says.

        The sender of a message, the person who pressed a button, or who changed the bot's
        seat. ``None`` for deleted messages, for a channel post in the channel's name, for a
        seat change that does not say who, and for a kind this version does not know.
        """
        if self.message is not None:
            return self.message.from_user
        if self.callback_query is not None:
            return self.callback_query.from_user
        if self.chat_member is not None:
            return self.chat_member.from_user
        return None

    @property
    def event(self) -> Any:
        """The event the update carries, whichever field holds it.

        A `MessageBase`, a `CallbackQueryBase`, a `DeletedMessages` or a `ChatMemberUpdate`,
        as `kind` says. ``None`` for a kind this version does not know.
        """
        return self.message or self.callback_query or self.deleted or self.chat_member

    @classmethod
    def from_json(cls, data: Any) -> Update:
        """Builds an update, its event included, from the API's JSON.

        The bot does this for every update it fetches or is sent. The event is built as a
        plain `MessageBase`, `CallbackQueryBase`, `DeletedMessages` or `ChatMemberUpdate`:
        attached to no bot, and not decrypted.

        Args:
            data: One update, as ``GET /updates`` lists them or a webhook posts it.

        Returns:
            The update, with ``data`` kept in ``raw``. A missing ``kind`` is read as
            ``"unknown"``, and an update of a kind this version does not know has no event.

        Raises:
            PayloadError: ``data`` is not an object; its ``updateId`` is missing or not an
                integer; or the event its ``kind`` calls for is missing or not valid (see
                `MessageBase.from_json`, `CallbackQueryBase.from_json`, `ChatRef.from_json`
                and `Seat.from_json`).
        """
        d = _obj(data, "update")
        update_id = _req_int(d, "updateId", "update")
        kind = UpdateKind(_str(d, "kind") or "unknown")
        values: dict[str, Any] = {}
        if kind in (UpdateKind.MESSAGE, UpdateKind.EDITED_MESSAGE):
            values["message"] = MessageBase.from_json(d.get("message"))
        elif kind == UpdateKind.CALLBACK_QUERY:
            values["callback_query"] = CallbackQueryBase.from_json(d.get("callback"))
        elif kind == UpdateKind.DELETED_MESSAGES:
            ids = tuple(i for i in _list(d, "messageIds") if isinstance(i, int) and not isinstance(i, bool))
            values["deleted"] = DeletedMessages(chat=ChatRef.from_json(d.get("chat")), message_ids=ids, raw=d)
        elif kind == UpdateKind.MY_CHAT_MEMBER:
            sender = d.get("from")
            values["chat_member"] = ChatMemberUpdate(
                chat=ChatRef.from_json(d.get("chat")),
                from_user=User.from_json(sender) if isinstance(sender, Mapping) else None,
                date=_when(d, "date"),
                old=Seat.from_json(d.get("old")),
                new=Seat.from_json(d.get("new")),
                raw=d,
            )
        return cls(update_id=update_id, kind=kind, **values, raw=d)


# --- the acting flavours ---
#
# Same fields as the bases, plus methods that go through the bot that received
# them. Bot hands out the async ones, SyncBot the sync ones; parity between the
# two is checked by the test suite.


def _need(bot: Any) -> Any:
    if bot is None:
        raise RuntimeError("this object is not bound to a bot: it was not handed out by Bot or SyncBot")
    return bot


@dataclass(frozen=True, slots=True, kw_only=True)
class Message(MessageBase):
    """A received message whose methods use the `Bot` that got it."""

    @classmethod
    def bind(cls, message: MessageBase, bot: Bot) -> Message:
        """Copies a plain `MessageBase` into one that acts through ``bot``.

        The bot does this to every message before a handler sees it. You need it yourself only
        for a message you built or read back from somewhere, in a test for example.

        Args:
            message: The message as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `Message` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    async def reply(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        quote: bool = True,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SentMessage:
        """Answers in the same chat, quoting this message unless ``quote=False``.

        A send like any other: `Bot.send_message` does the work, so the answer is sealed and
        signed when the chat is end-to-end encrypted, and the same errors come out of it.

        Args:
            text: The answer, with the rules of `Bot.send_message`.
            markup: Buttons under the answer. Never encrypted.
            quote: ``False`` writes into the chat without quoting this message.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SentMessage` as the server stored it, which can be edited or deleted.

        Raises:
            RuntimeError: The message was not handed out by a `Bot`, so it has nothing to
                answer through.
        """
        sent: SentMessage = await _need(self._bot).send_message(
            self.chat.id,
            text,
            reply_to=self.id if quote else None,
            markup=markup,
            silent=silent,
            encrypt=encrypt,
            send_id=send_id,
        )
        return sent

    async def answer(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SentMessage:
        """Writes into the same chat without quoting: `reply` with ``quote=False``.

        Args:
            text: The message, with the rules of `Bot.send_message`.
            markup: Buttons under it. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SentMessage` as the server stored it.

        Raises:
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return await self.reply(text, markup=markup, quote=False, silent=silent, encrypt=encrypt, send_id=send_id)

    async def delete(self) -> bool:
        """Deletes this message for everyone. Needs the *delete messages* right in a group.

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The bot may not delete other people's messages here.
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).delete_message(self.chat.id, self.id))

    async def typing(self, typing: bool = True) -> bool:
        """Shows "typing..." in this chat for a few seconds.

        Args:
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator was shown, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            RuntimeError: The message was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).send_typing(self.chat.id, typing))


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncMessage(MessageBase):
    """A received message whose methods use the `SyncBot` that got it."""

    @classmethod
    def bind(cls, message: MessageBase, bot: SyncBot) -> SyncMessage:
        """Copies a plain `MessageBase` into one that acts through ``bot``.

        The bot does this to every message before a handler sees it. You need it yourself only
        for a message you built or read back from somewhere, in a test for example.

        Args:
            message: The message as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SyncMessage` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    def reply(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        quote: bool = True,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SyncSentMessage:
        """Answers in the same chat, quoting this message unless ``quote=False``.

        A send like any other: `SyncBot.send_message` does the work, so the answer is sealed
        and signed when the chat is end-to-end encrypted, and the same errors come out of it.

        Args:
            text: The answer, with the rules of `SyncBot.send_message`.
            markup: Buttons under the answer. Never encrypted.
            quote: ``False`` writes into the chat without quoting this message.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` as the server stored it, which can be edited or deleted.

        Raises:
            RuntimeError: The message was not handed out by a `SyncBot`, so it has nothing to
                answer through.
        """
        return _need(self._bot).send_message(  # type: ignore[no-any-return]
            self.chat.id,
            text,
            reply_to=self.id if quote else None,
            markup=markup,
            silent=silent,
            encrypt=encrypt,
            send_id=send_id,
        )

    def answer(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        silent: bool = False,
        encrypt: bool | None = None,
        send_id: str | None = None,
    ) -> SyncSentMessage:
        """Writes into the same chat without quoting: `reply` with ``quote=False``.

        Args:
            text: The message, with the rules of `SyncBot.send_message`.
            markup: Buttons under it. Never encrypted.
            silent: ``True`` posts without a notification. It only matters in channels.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.
            send_id: A UUID that makes this send safe to repeat. One is made for you.

        Returns:
            The `SyncSentMessage` as the server stored it.

        Raises:
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return self.reply(text, markup=markup, quote=False, silent=silent, encrypt=encrypt, send_id=send_id)

    def delete(self) -> bool:
        """Deletes this message for everyone. Needs the *delete messages* right in a group.

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The bot may not delete other people's messages here.
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).delete_message(self.chat.id, self.id))

    def typing(self, typing: bool = True) -> bool:
        """Shows "typing..." in this chat for a few seconds.

        Args:
            typing: ``False`` clears the indicator early.

        Returns:
            ``True`` if the indicator was shown, ``False`` if one went out to this chat less
            than 3 seconds ago.

        Raises:
            RuntimeError: The message was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).send_typing(self.chat.id, typing))


@dataclass(frozen=True, slots=True, kw_only=True)
class SentMessage(SentMessageBase):
    """One of the bot's messages, with `edit()` and `delete()`."""

    @classmethod
    def bind(cls, message: SentMessageBase, bot: Bot) -> SentMessage:
        """Copies a plain `SentMessageBase` into one that acts through ``bot``.

        The methods that send do this to what they return, so you rarely call it yourself.

        Args:
            message: The message as the API gave it back.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SentMessage` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    async def edit(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """New words, new buttons, or both: `Bot.edit_message` on this message.

        Args:
            text: The new text, with the rules of `Bot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP`, the default, keeps them; ``None`` removes them;
                a keyboard replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ValidationError: There is nothing to change: no ``text``, and ``markup`` left at
                :data:`KEEP`.
            RuntimeError: The message did not come from a `Bot`.
        """
        return await _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.id, text, markup=markup, encrypt=encrypt
        )

    async def delete(self) -> bool:
        """Deletes this message for everyone. A bot may always delete its own.

        Returns:
            ``True`` once the message is gone.

        Raises:
            MessageNotFoundError: It was deleted already.
            RuntimeError: The message did not come from a `Bot`.
        """
        return bool(await _need(self._bot).delete_message(self.chat.id, self.id))


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncSentMessage(SentMessageBase):
    """One of the bot's messages, with `edit()` and `delete()` (sync)."""

    @classmethod
    def bind(cls, message: SentMessageBase, bot: SyncBot) -> SyncSentMessage:
        """Copies a plain `SentMessageBase` into one that acts through ``bot``.

        The methods that send do this to what they return, so you rarely call it yourself.

        Args:
            message: The message as the API gave it back.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SyncSentMessage` bound to ``bot``.
        """
        return _rebind(cls, message, bot)

    def edit(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """New words, new buttons, or both: `SyncBot.edit_message` on this message.

        Args:
            text: The new text, with the rules of `SyncBot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP`, the default, keeps them; ``None`` removes them;
                a keyboard replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ValidationError: There is nothing to change: no ``text``, and ``markup`` left at
                :data:`KEEP`.
            RuntimeError: The message did not come from a `SyncBot`.
        """
        return _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.id, text, markup=markup, encrypt=encrypt
        )

    def delete(self) -> bool:
        """Deletes this message for everyone. A bot may always delete its own.

        Returns:
            ``True`` once the message is gone.

        Raises:
            MessageNotFoundError: It was deleted already.
            RuntimeError: The message did not come from a `SyncBot`.
        """
        return bool(_need(self._bot).delete_message(self.chat.id, self.id))


@dataclass(frozen=True, slots=True, kw_only=True)
class CallbackQuery(CallbackQueryBase):
    """A button press, with `answer()` and helpers to change the message."""

    @classmethod
    def bind(cls, query: CallbackQueryBase, bot: Bot) -> CallbackQuery:
        """Copies a plain `CallbackQueryBase` into one that acts through ``bot``.

        The bot does this to every press before a handler sees it.

        Args:
            query: The press as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `CallbackQuery` bound to ``bot``.
        """
        return _rebind(cls, query, bot)

    async def answer(self, text: str | None = None, *, alert: bool = False, url: str | None = None) -> bool:
        """Stops the spinner. ``text`` flashes a line, ``alert=True`` shows a box, ``url`` offers a page.

        Once per press, within 60 seconds of it. The bot keeps no spinner turning: a press no
        handler answered gets an empty answer when the update is done.

        Args:
            text: Up to 200 characters on one line. ``None`` shows nothing.
            alert: ``True`` shows ``text`` in a box the person has to close.
            url: An ``http`` or ``https`` address the phone offers to open.

        Returns:
            ``True``. Answering twice raises instead.

        Raises:
            CallbackExpiredError: More than 60 seconds have passed since the press.
            CallbackAnsweredError: This press was answered already.
            RuntimeError: The press was not handed out by a `Bot`.

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        return bool(await _need(self._bot).answer_callback(self.id, text, alert=alert, url=url))

    async def edit_message(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """Edits the message the pressed button sits under.

        Args:
            text: The new text, with the rules of `Bot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP` keeps them, ``None`` removes them, a keyboard
                replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ForbiddenError: The buttons are under somebody else's message, so the bot may not
                edit it.
            RuntimeError: The press was not handed out by a `Bot`.
        """
        return await _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.message_id, text, markup=markup, encrypt=encrypt
        )

    async def delete_message(self) -> bool:
        """Deletes the message the pressed button sits under.

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The message is not the bot's own and it has no right to delete it.
            RuntimeError: The press was not handed out by a `Bot`.
        """
        return bool(await _need(self._bot).delete_message(self.chat.id, self.message_id))

    async def send(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        encrypt: bool | None = None,
    ) -> SentMessage:
        """Sends a new message into the chat the button was pressed in.

        Args:
            text: The message, with the rules of `Bot.send_message`.
            markup: Buttons under it. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The `SentMessage` as the server stored it.

        Raises:
            RuntimeError: The press was not handed out by a `Bot`.
        """
        return await _need(self._bot).send_message(  # type: ignore[no-any-return]
            self.chat.id, text, markup=markup, encrypt=encrypt
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncCallbackQuery(CallbackQueryBase):
    """A button press, with `answer()` and helpers to change the message (sync)."""

    @classmethod
    def bind(cls, query: CallbackQueryBase, bot: SyncBot) -> SyncCallbackQuery:
        """Copies a plain `CallbackQueryBase` into one that acts through ``bot``.

        The bot does this to every press before a handler sees it.

        Args:
            query: The press as the API gave it.
            bot: The bot its methods will go through.

        Returns:
            The same fields, as a `SyncCallbackQuery` bound to ``bot``.
        """
        return _rebind(cls, query, bot)

    def answer(self, text: str | None = None, *, alert: bool = False, url: str | None = None) -> bool:
        """Stops the spinner. ``text`` flashes a line, ``alert=True`` shows a box, ``url`` offers a page.

        Once per press, within 60 seconds of it. The bot keeps no spinner turning: a press no
        handler answered gets an empty answer when the update is done.

        Args:
            text: Up to 200 characters on one line. ``None`` shows nothing.
            alert: ``True`` shows ``text`` in a box the person has to close.
            url: An ``http`` or ``https`` address the phone offers to open.

        Returns:
            ``True``. Answering twice raises instead.

        Raises:
            CallbackExpiredError: More than 60 seconds have passed since the press.
            CallbackAnsweredError: This press was answered already.
            RuntimeError: The press was not handed out by a `SyncBot`.

        Warning:
            Answers are not encrypted, even in an encrypted chat.
        """
        return bool(_need(self._bot).answer_callback(self.id, text, alert=alert, url=url))

    def edit_message(
        self,
        text: str | None = None,
        *,
        markup: InlineKeyboard | None = KEEP,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """Edits the message the pressed button sits under.

        Args:
            text: The new text, with the rules of `SyncBot.send_message`. ``None`` keeps it.
            markup: The buttons. :data:`KEEP` keeps them, ``None`` removes them, a keyboard
                replaces them. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The edited message, as the server stored it.

        Raises:
            ForbiddenError: The buttons are under somebody else's message, so the bot may not
                edit it.
            RuntimeError: The press was not handed out by a `SyncBot`.
        """
        return _need(self._bot).edit_message(  # type: ignore[no-any-return]
            self.chat.id, self.message_id, text, markup=markup, encrypt=encrypt
        )

    def delete_message(self) -> bool:
        """Deletes the message the pressed button sits under.

        Returns:
            ``True`` once the message is gone.

        Raises:
            ForbiddenError: The message is not the bot's own and it has no right to delete it.
            RuntimeError: The press was not handed out by a `SyncBot`.
        """
        return bool(_need(self._bot).delete_message(self.chat.id, self.message_id))

    def send(
        self,
        text: str,
        *,
        markup: InlineKeyboard | None = None,
        encrypt: bool | None = None,
    ) -> SyncSentMessage:
        """Sends a new message into the chat the button was pressed in.

        Args:
            text: The message, with the rules of `SyncBot.send_message`.
            markup: Buttons under it. Never encrypted.
            encrypt: ``None`` follows the chat; ``True`` refuses to write in clear, ``False``
                refuses to write sealed.

        Returns:
            The `SyncSentMessage` as the server stored it.

        Raises:
            RuntimeError: The press was not handed out by a `SyncBot`.
        """
        return _need(self._bot).send_message(  # type: ignore[no-any-return]
            self.chat.id, text, markup=markup, encrypt=encrypt
        )


def updates_from_json(items: Sequence[Any]) -> list[Update]:
    return [Update.from_json(item) for item in items]
