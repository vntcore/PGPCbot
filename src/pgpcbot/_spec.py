"""Every operation of `openapi.json`, described once.

Each function checks its arguments the way the server would and returns a
`Call`; `pgpcbot.client` sends it. The names
follow the operationIds (`getMe` -> `get_me()`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import _validate as v
from ._http import Call
from .errors import PayloadError, ValidationError
from .keyboard import InlineKeyboard, markup_payload
from .models import (
    KEEP,
    BotInfo,
    Chat,
    ChatKeys,
    ChatKind,
    ChatMembers,
    Command,
    CommandScope,
    Member,
    PublishedKey,
    Resolved,
    SentMessageBase,
    Update,
    UserKey,
    WebhookInfo,
)


def _flag(result: Any, key: str) -> bool:
    if not isinstance(result, Mapping):
        raise PayloadError(f"expected an object with {key!r}, got {type(result).__name__}")
    return result.get(key) is True


def _updates(result: Any) -> list[Update]:
    if not isinstance(result, list):
        raise PayloadError("updates: expected a list")
    return [Update.from_json(item) for item in result]


def _raw_updates(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        raise PayloadError("updates: expected a list")
    return [item for item in result if isinstance(item, dict)]


def _scopes(result: Any) -> list[CommandScope]:
    if not isinstance(result, list):
        raise PayloadError("commands: expected a list")
    return [CommandScope.from_json(item) for item in result]


def _commands(result: Any) -> list[Command]:
    if not isinstance(result, list):
        raise PayloadError("commands: expected a list")
    return [Command.from_json(item) for item in result]


# --- Me ---


def get_me() -> Call[BotInfo]:
    return Call("GET", "/me", BotInfo.from_json)


# --- Updates ---


def get_updates(
    offset: int | None = None, limit: int | None = None, timeout: int | None = None, *, raw: bool = False
) -> Call[Any]:
    params = v.check_poll(offset, limit, timeout)
    # a long poll must outlive its own timeout on our side, or we hang up first
    read = float(timeout) + 15.0 if timeout else None
    # not repeatable here: the poller decides what to do with a lost poll
    return Call(
        "GET", "/updates", _raw_updates if raw else _updates, params=params, read_timeout=read, repeatable=False
    )


def get_webhook() -> Call[WebhookInfo]:
    return Call("GET", "/webhook", WebhookInfo.from_json)


def set_webhook(url: str, secret: str | None = None) -> Call[WebhookInfo]:
    body: dict[str, Any] = {"url": v.check_webhook_url(url)}
    if secret is not None:
        body["secret"] = v.check_secret(secret)
    return Call("POST", "/webhook", WebhookInfo.from_json, body=body, has_body=True)


def delete_webhook() -> Call[WebhookInfo]:
    return Call("DELETE", "/webhook", WebhookInfo.from_json)


# --- Key ---


def set_key(public_key: str, fingerprint: str) -> Call[PublishedKey]:
    body = {"publicKey": v.check_public_key(public_key), "fingerprint": v.check_fingerprint(fingerprint)}
    return Call("PUT", "/key", PublishedKey.from_json, body=body, has_body=True)


# --- Commands ---


def get_commands(scope: str | None = None) -> Call[Any]:
    if scope is None:
        return Call("GET", "/commands", _scopes)
    return Call("GET", "/commands", _commands, params={"scope": v.check_scope(scope)})


def _command_list(commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    items = list(commands)
    if len(items) > v.COMMANDS_PER_SCOPE:
        raise ValidationError(f"at most {v.COMMANDS_PER_SCOPE} commands per scope", field="commands")
    for item in items:
        given: object = item
        name: object
        description: object
        if isinstance(given, Command):
            name, description = given.command, given.description
        elif isinstance(given, Mapping):
            name, description = given.get("command"), given.get("description")
        elif isinstance(given, tuple) and len(given) == 2:
            name, description = given
        else:
            raise ValidationError(
                f"a command is a Command, a dict or a (name, description) pair, got {item!r}", field="commands"
            )
        clean = v.clean_command(name)
        text = v.clean_command_description(description, clean)
        # the server folds duplicates to the first one; doing it here keeps the echo identical
        if clean in seen:
            continue
        seen.add(clean)
        out.append({"command": clean, "description": text})
    return out


def set_commands(
    commands: Iterable[Command | Mapping[str, Any] | tuple[str, str]], scope: str | None = None
) -> Call[CommandScope]:
    body: dict[str, Any] = {"commands": _command_list(commands)}
    if scope is not None:
        body["scope"] = v.check_scope(scope)
    return Call("PUT", "/commands", CommandScope.from_json, body=body, has_body=True)


def delete_commands(scope: str | None = None) -> Call[CommandScope]:
    params = {"scope": v.check_scope(scope)} if scope is not None else None
    return Call("DELETE", "/commands", CommandScope.from_json, params=params)


# --- Messages ---


def _body(text: str | None, armored: str | None, *, required: bool, text_max: int) -> dict[str, str]:
    if text is not None and armored is not None:
        raise ValidationError("send either text or armored, not both")
    if text is not None:
        return {"text": v.check_text(text, limit=text_max)}
    if armored is not None:
        return {"armored": v.check_armored(armored)}
    if required:
        raise ValidationError("text or armored is required", field="text")
    return {}


def send_message(
    chat_id: int,
    text: str | None = None,
    *,
    armored: str | None = None,
    reply_to: int | None = None,
    markup: InlineKeyboard | Mapping[str, Any] | None = None,
    silent: bool | None = None,
    send_id: str | uuid.UUID | None = None,
    chat_kind: ChatKind | None = None,
    text_max: int = v.TEXT_MAX,
) -> Call[SentMessageBase]:
    body: dict[str, Any] = {"chatId": v.check_id(chat_id, "chat_id")}
    body.update(_body(text, armored, required=True, text_max=text_max))
    if reply_to is not None:
        body["replyTo"] = v.check_id(reply_to, "reply_to")
    payload = markup_payload(markup)
    if payload is not None:
        body["markup"] = payload
    if silent is not None:
        body["silent"] = bool(silent)
    # always one: it is what makes a retry after a timeout write nothing twice
    body["sendId"] = v.check_send_id(send_id) if send_id is not None else str(uuid.uuid4())
    return Call(
        "POST",
        "/messages",
        SentMessageBase.from_json,
        body=body,
        has_body=True,
        send=True,
        chat_id=chat_id,
        chat_kind=chat_kind,
    )


def edit_message(
    chat_id: int,
    message_id: int,
    text: str | None = None,
    *,
    armored: str | None = None,
    markup: InlineKeyboard | Mapping[str, Any] | None = KEEP,
    chat_kind: ChatKind | None = None,
    text_max: int = v.TEXT_MAX,
) -> Call[SentMessageBase]:
    message_id = v.check_id(message_id, "message_id")
    body: dict[str, Any] = {"chatId": v.check_id(chat_id, "chat_id")}
    body.update(_body(text, armored, required=False, text_max=text_max))
    if markup is not KEEP:
        # null is meaningful here: it takes the buttons away
        body["markup"] = markup_payload(markup)
    if len(body) == 1:
        raise ValidationError("nothing to change: give text, armored or markup")
    return Call(
        "PATCH",
        f"/messages/{message_id}",
        SentMessageBase.from_json,
        body=body,
        has_body=True,
        send=True,
        edit=True,
        chat_id=chat_id,
        chat_kind=chat_kind,
    )


def delete_message(chat_id: int, message_id: int) -> Call[bool]:
    message_id = v.check_id(message_id, "message_id")
    # chatId in the query: a body on DELETE is legal but some proxies drop it
    return Call(
        "DELETE",
        f"/messages/{message_id}",
        lambda r: _flag(r, "deleted"),
        params={"chatId": v.check_id(chat_id, "chat_id")},
        done_codes=frozenset({"message_not_found"}),
        done_value=True,
    )


# --- Buttons ---


def answer_callback(
    callback_id: str, text: str | None = None, *, alert: bool = False, url: str | None = None
) -> Call[bool]:
    given: object = callback_id
    # "." and ".." would be read as path steps and send the call somewhere else
    if not isinstance(given, str) or not given or len(given) > 64 or given in (".", ".."):
        raise ValidationError("callback_id must be the id of a callback query", field="callback_id")
    clean, url = v.check_callback_answer(text, url)
    body: dict[str, Any] = {}
    if clean is not None:
        body["text"] = clean
    if alert:
        body["alert"] = True
    if url is not None:
        body["url"] = url
    return Call(
        "POST",
        f"/callbacks/{_path_segment(callback_id)}/answer",
        lambda r: _flag(r, "answered"),
        body=body,
        has_body=True,
        done_codes=frozenset({"callback_answered"}),
        done_value=True,
    )


def _path_segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


# --- Chats ---


def get_chat(chat_id: int) -> Call[Chat]:
    return Call("GET", f"/chats/{v.check_id(chat_id, 'chat_id')}", Chat.from_json)


def get_chat_keys(chat_id: int) -> Call[ChatKeys]:
    return Call("GET", f"/chats/{v.check_id(chat_id, 'chat_id')}/keys", ChatKeys.from_json)


def get_chat_members(chat_id: int) -> Call[ChatMembers]:
    return Call("GET", f"/chats/{v.check_id(chat_id, 'chat_id')}/members", ChatMembers.from_json)


def get_chat_member(chat_id: int, user_id: int) -> Call[Member]:
    return Call(
        "GET",
        f"/chats/{v.check_id(chat_id, 'chat_id')}/members/{v.check_id(user_id, 'user_id')}",
        Member.from_json,
    )


def resolve(username: str) -> Call[Resolved]:
    return Call("GET", f"/resolve/{v.check_username(username)}", Resolved.from_json)


def leave_chat(chat_id: int) -> Call[bool]:
    return Call(
        "POST",
        f"/chats/{v.check_id(chat_id, 'chat_id')}/leave",
        lambda r: _flag(r, "left"),
        # gone from the chat: a repeat of a leave that worked finds no chat
        done_codes=frozenset({"chat_not_found"}),
        done_value=True,
    )


def send_typing(chat_id: int, typing: bool = True) -> Call[bool]:
    # a typing indicator three seconds late is worse than none: no retries
    return Call(
        "POST",
        f"/chats/{v.check_id(chat_id, 'chat_id')}/typing",
        lambda r: _flag(r, "sent"),
        body={"typing": bool(typing)},
        has_body=True,
        repeatable=False,
    )


def get_user_key(user_id: int) -> Call[UserKey]:
    return Call("GET", f"/users/{v.check_id(user_id, 'user_id')}/key", UserKey.from_json)


# --- Token ---


def revoke_token() -> Call[bool]:
    return Call(
        "POST",
        "/token/revoke",
        lambda r: _flag(r, "revoked"),
        # the first one worked if the token is dead by the time we ask again
        done_codes=frozenset({"unauthorized"}),
        done_value=True,
    )


OPERATIONS: Sequence[str] = (
    "get_me",
    "get_updates",
    "get_webhook",
    "set_webhook",
    "delete_webhook",
    "set_key",
    "get_commands",
    "set_commands",
    "delete_commands",
    "send_message",
    "edit_message",
    "delete_message",
    "answer_callback",
    "get_chat",
    "get_chat_keys",
    "get_chat_members",
    "get_chat_member",
    "leave_chat",
    "send_typing",
    "resolve",
    "get_user_key",
    "revoke_token",
)
