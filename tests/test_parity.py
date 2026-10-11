from __future__ import annotations

import dataclasses
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from pgpcbot import AsyncClient, Bot, Client, SyncBot, _http, _spec
from pgpcbot.models import (
    CallbackQuery,
    Message,
    SentMessage,
    SyncCallbackQuery,
    SyncMessage,
    SyncSentMessage,
)

from .conftest import fixture_text

OPENAPI: dict[str, Any] = json.loads((Path(__file__).parents[1] / "docs" / "openapi.json").read_text(encoding="utf-8"))

# what the blocking twin of each kind of callable has to be
BLOCKING_TWIN = {"coroutine": "plain", "async generator": "generator", "generator": "generator", "plain": "plain"}

CONTEXT_MANAGER = frozenset({"__aenter__", "__aexit__", "__enter__", "__exit__"})

# Where Bot and SyncBot differ on purpose. SyncBot runs its own event loop, so it
# has no start(), start_polling() or "async with" meant for a loop you own; stop()
# is a coroutine on both because the loop is what calls it; lifespan() is the one
# async context manager both hand to an ASGI server; process_update() awaits on
# Bot and blocks on SyncBot; run() blocks on both.
ENTRY_POINTS = frozenset({"run", "start_polling", "start", "process_update", "lifespan", "__aenter__", "__aexit__"})


def kind_of(function: Any) -> str:
    if inspect.iscoroutinefunction(function):
        return "coroutine"
    if inspect.isasyncgenfunction(function):
        return "async generator"
    if inspect.isgeneratorfunction(function):
        return "generator"
    return "plain"


def parameters(function: Any) -> list[tuple[str, Any, Any]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def methods(cls: type) -> dict[str, tuple[str, list[tuple[str, Any, Any]]]]:
    """The public methods of ``cls``, inherited ones included, with how each one is called."""
    found = {}
    for name in dir(cls):
        if name.startswith("_") and name not in CONTEXT_MANAGER:
            continue
        static = inspect.getattr_static(cls, name)
        function = static.__func__ if isinstance(static, (classmethod, staticmethod)) else static
        if isinstance(static, property) or not callable(function):
            continue
        found[name] = (kind_of(function), parameters(getattr(cls, name)))
    return found


def properties(cls: type) -> set[str]:
    return {
        name
        for name in dir(cls)
        if not name.startswith("_") and isinstance(inspect.getattr_static(cls, name), property)
    }


def mismatches(
    awaiting: type, blocking: type, *, renamed: dict[str, str] | None = None, skip: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Every method where ``blocking`` is not the twin of ``awaiting``, and why."""
    renamed = renamed or {}
    left = {renamed.get(name, name): spec for name, spec in methods(awaiting).items() if name not in skip}
    right = {name: spec for name, spec in methods(blocking).items() if name not in skip}
    found: dict[str, Any] = {}
    for name in sorted(left.keys() | right.keys()):
        if name not in right:
            found[name] = f"only on {awaiting.__name__}"
        elif name not in left:
            found[name] = f"only on {blocking.__name__}"
        elif BLOCKING_TWIN[left[name][0]] != right[name][0]:
            found[name] = f"{left[name][0]} on {awaiting.__name__}, {right[name][0]} on {blocking.__name__}"
        elif left[name][1] != right[name][1]:
            found[name] = {"async": left[name][1], "sync": right[name][1]}
    return found


def snake(operation_id: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", operation_id).lower()


def resolve(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        target: Any = OPENAPI
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


SPEC_OPERATIONS = [
    pytest.param(method.upper(), path, operation, id=operation["operationId"])
    for path, item in OPENAPI["paths"].items()
    for method, operation in item.items()
    if isinstance(operation, dict) and "operationId" in operation
]

# arguments that fill in everything each operation can put on the wire
SAMPLES: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {
    "get_me": ((), {}),
    "get_updates": ((10, 100, 50), {}),
    "get_webhook": ((), {}),
    "set_webhook": (("https://bot.example/hook", "s3cret"), {}),
    "delete_webhook": ((), {}),
    "set_key": ((fixture_text("bot.pub.asc"), "3F2A9C1E5D7B4A6E8C0F1B2D3E4F5A6B7C8D9E0F"), {}),
    "get_commands": (("private",), {}),
    "set_commands": (([("start", "Say hello")], "private"), {}),
    "delete_commands": (("private",), {}),
    "send_message": (
        (7781, "hello"),
        {
            "reply_to": 1000,
            "markup": {"inlineKeyboard": [[{"text": "Hourly", "callbackData": "hourly"}]]},
            "silent": True,
            "send_id": "6f9619ff-8b86-4011-b42d-00c04fc964ff",
        },
    ),
    "edit_message": ((7781, 1001, "hello again"), {"markup": None}),
    "delete_message": ((7781, 1001), {}),
    "answer_callback": (("4f1c9b2e7a3d4c8e9b0a1f", "done"), {"alert": True, "url": "https://bot.example/more"}),
    "get_chat": ((7781,), {}),
    "get_chat_keys": ((7781,), {}),
    "get_chat_members": ((9100,), {}),
    "get_chat_member": ((9100, 55), {}),
    "leave_chat": ((9100,), {}),
    "send_typing": ((7781, False), {}),
    "resolve": (("@anna",), {}),
    "get_user_key": ((55,), {}),
    "revoke_token": ((), {}),
}


def test_the_two_clients_are_twins() -> None:
    renamed = {"aclose": "close", "__aenter__": "__enter__", "__aexit__": "__exit__"}
    assert mismatches(AsyncClient, Client, renamed=renamed) == {}
    assert properties(AsyncClient) == properties(Client)
    # plain on both sides would pass as twins: the downloads have to await on one of them
    assert (kind_of(AsyncClient.download_media), kind_of(Client.download_media)) == ("coroutine", "plain")


def test_bot_and_sync_bot_are_twins_apart_from_how_they_start_and_stop() -> None:
    assert mismatches(Bot, SyncBot, skip=ENTRY_POINTS) == {}
    assert properties(Bot) == properties(SyncBot)


def test_what_bot_does_over_the_api_it_awaits() -> None:
    over_the_api = sorted(name for name in methods(Bot) if not name.startswith("_") and hasattr(AsyncClient, name))
    assert len(over_the_api) > 15
    assert {name: kind_of(getattr(Bot, name)) for name in over_the_api} == dict.fromkeys(over_the_api, "coroutine")


def test_the_entry_points_differ_only_as_documented() -> None:
    assert kind_of(Bot.run) == kind_of(SyncBot.run) == "plain"
    assert parameters(Bot.run) == parameters(SyncBot.run)
    assert (kind_of(Bot.process_update), kind_of(SyncBot.process_update)) == ("coroutine", "plain")
    assert parameters(Bot.process_update) == parameters(SyncBot.process_update)
    # stop() follows the rule (awaited on Bot, plain on SyncBot); request_stop() is plain on both
    assert (kind_of(Bot.stop), kind_of(SyncBot.stop)) == ("coroutine", "plain")
    assert kind_of(Bot.request_stop) == kind_of(SyncBot.request_stop) == "plain"
    assert Bot.lifespan is SyncBot.lifespan
    for name in ("start", "start_polling", "__aenter__", "__aexit__"):
        assert kind_of(getattr(Bot, name)) == "coroutine", name
        assert not hasattr(SyncBot, name), name


@pytest.mark.parametrize(
    ("awaiting", "blocking"),
    [(Message, SyncMessage), (SentMessage, SyncSentMessage), (CallbackQuery, SyncCallbackQuery)],
    ids=["Message", "SentMessage", "CallbackQuery"],
)
def test_what_a_handler_receives_is_the_same_object_in_both_flavours(awaiting: type, blocking: type) -> None:
    assert mismatches(awaiting, blocking) == {}
    assert properties(awaiting) == properties(blocking)
    assert [f.name for f in dataclasses.fields(awaiting)] == [f.name for f in dataclasses.fields(blocking)]
    acting = [name for name, value in vars(awaiting).items() if not name.startswith("_") and inspect.isfunction(value)]
    assert acting
    assert {name: kind_of(getattr(awaiting, name)) for name in acting} == dict.fromkeys(acting, "coroutine")


@pytest.mark.parametrize("operation_id", [param.id for param in SPEC_OPERATIONS])
def test_every_operation_of_the_spec_is_a_method_of_both_clients(operation_id: str) -> None:
    name = snake(operation_id)
    assert name in _spec.OPERATIONS
    assert callable(getattr(AsyncClient, name, None))
    assert callable(getattr(Client, name, None))
    assert (kind_of(getattr(AsyncClient, name)), kind_of(getattr(Client, name))) == ("coroutine", "plain")


def test_the_spec_layer_lists_exactly_the_operations_of_the_spec() -> None:
    names = [snake(str(param.id)) for param in SPEC_OPERATIONS]
    assert sorted(_spec.OPERATIONS) == sorted(names)
    assert len(set(_spec.OPERATIONS)) == len(_spec.OPERATIONS)
    assert OPENAPI["servers"][0]["url"] == "{apiUrl}" + _http.API_PREFIX


@pytest.mark.parametrize(("method", "path", "operation"), SPEC_OPERATIONS)
def test_each_operation_is_built_the_way_the_spec_declares_it(
    method: str, path: str, operation: dict[str, Any]
) -> None:
    name = snake(operation["operationId"])
    args, kwargs = SAMPLES[name]
    call = getattr(_spec, name)(*args, **kwargs)
    assert call.method == method
    assert re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", path), call.path), call.path

    declared = [resolve(p) for p in (*OPENAPI["paths"][path].get("parameters", ()), *operation.get("parameters", ()))]
    assert set(call.params or {}) == {p["name"] for p in declared if p["in"] == "query"}

    body = operation.get("requestBody")
    if call.has_body:
        assert body is not None
        schema = resolve(body["content"]["application/json"]["schema"])
        assert set(call.body) <= set(schema["properties"])
        assert set(schema.get("required", ())) <= set(call.body)
    else:
        assert body is None or not body.get("required", False)
