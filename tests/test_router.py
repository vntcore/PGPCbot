from __future__ import annotations

import inspect
from typing import Any

import pytest

from pgpcbot import CallbackQueryBase, MessageBase, Router, SkipHandler, UpdateKind
from pgpcbot import filters as f
from pgpcbot.filters import FilterContext, merge
from pgpcbot.router import ErrorHandler, Handler, is_async

CHATS = {
    "private": {"id": 7, "kind": "private"},
    "group": {"id": 300, "kind": "group", "title": "Dev"},
    "channel": {"id": 900, "kind": "channel", "title": "News"},
}


def message(text: str | None = "hello", chat: str = "private") -> MessageBase:
    return MessageBase.from_json(
        {"id": 1, "chat": CHATS[chat], "from": {"id": 55}, "date": 1791000000000, "kind": "user", "text": text}
    )


def press(data: str) -> CallbackQueryBase:
    return CallbackQueryBase.from_json(
        {"id": "q", "from": {"id": 55}, "chat": CHATS["private"], "messageId": 3, "data": data}
    )


async def accepts(handler: Handler, event: Any) -> dict[str, Any] | None:
    """What a dispatch would hand the handler, or None when its filters turn the event away."""
    context = FilterContext(bot_username="testbot", update=None, bot=None)
    data: dict[str, Any] = {}
    for flt in handler.filters:
        result = flt(event, context)
        if inspect.isawaitable(result):
            result = await result
        if not merge(result, data):
            return None
    return data


async def handler_a(event: Any) -> None: ...
async def handler_b(event: Any) -> None: ...
async def handler_c(event: Any) -> None: ...
async def handler_d(event: Any) -> None: ...
async def handler_e(event: Any) -> None: ...


async def mw_root_1(handler: Any, event: Any, data: Any) -> Any: ...
async def mw_root_2(handler: Any, event: Any, data: Any) -> Any: ...
async def mw_child(handler: Any, event: Any, data: Any) -> Any: ...
async def mw_leaf(handler: Any, event: Any, data: Any) -> Any: ...
async def mw_sibling(handler: Any, event: Any, data: Any) -> Any: ...


def names(pairs: Any) -> list[str]:
    return [handler.callback.__name__ for handler, _ in pairs]


def only(router: Router) -> Handler:
    (handler,) = router.handlers
    return handler


def test_router_names() -> None:
    assert Router("weather").name == "weather"
    anonymous = Router()
    assert anonymous.name == f"router-{id(anonymous):x}"
    assert repr(Router("w")) == "<Router w handlers=0 children=0>"


def test_include_router_returns_the_child() -> None:
    root, child = Router("root"), Router("child")
    assert root.include_router(child) is child
    assert repr(root) == "<Router root handlers=0 children=1>"


def test_a_router_cannot_include_itself() -> None:
    root = Router("root")
    with pytest.raises(ValueError, match="cannot include itself"):
        root.include_router(root)


def test_a_router_cannot_be_included_twice() -> None:
    root, other, child = Router("root"), Router("other"), Router("child")
    root.include_router(child)
    with pytest.raises(ValueError, match="child is already included in root"):
        root.include_router(child)
    with pytest.raises(ValueError, match="child is already included in root"):
        other.include_router(child)


def test_including_an_ancestor_would_make_a_loop() -> None:
    root, middle, leaf = Router("root"), Router("middle"), Router("leaf")
    root.include_router(middle)
    middle.include_router(leaf)
    with pytest.raises(ValueError, match="including root here would make a loop"):
        leaf.include_router(root)
    # the refused include changed nothing: root is still the top and walking it ends
    root.on_message()(handler_a)
    leaf.on_message()(handler_b)
    assert names(root.iter_handlers(UpdateKind.MESSAGE)) == ["handler_a", "handler_b"]
    assert names(leaf.iter_handlers(UpdateKind.MESSAGE)) == ["handler_b"]


def test_include_routers_keeps_the_order() -> None:
    root, a, b = Router("root"), Router("a"), Router("b")
    a.on_message()(handler_a)
    b.on_message()(handler_b)
    root.include_routers(a, b)
    assert names(root.iter_handlers(UpdateKind.MESSAGE)) == ["handler_a", "handler_b"]


@pytest.mark.parametrize(
    ("register", "kind"),
    [
        (lambda r: r.on_message(), UpdateKind.MESSAGE),
        (lambda r: r.on_edited_message(), UpdateKind.EDITED_MESSAGE),
        (lambda r: r.on_callback_query(), UpdateKind.CALLBACK_QUERY),
        (lambda r: r.on_deleted_messages(), UpdateKind.DELETED_MESSAGES),
        (lambda r: r.on_chat_member(), UpdateKind.MY_CHAT_MEMBER),
        (lambda r: r.command("start"), UpdateKind.MESSAGE),
        (lambda r: r.on_update(), None),
    ],
)
def test_each_decorator_registers_its_kind_and_returns_the_function(register: Any, kind: UpdateKind | None) -> None:
    router = Router()
    assert register(router)(handler_a) is handler_a
    handler = only(router)
    assert handler.callback is handler_a
    assert handler.kind == kind


def test_a_function_can_be_registered_for_several_kinds() -> None:
    router = Router()

    @router.on_message()
    @router.on_edited_message()
    async def both(event: Any) -> None: ...

    assert [h.kind for h in router.handlers] == [UpdateKind.EDITED_MESSAGE, UpdateKind.MESSAGE]


def test_a_filter_that_is_not_callable_is_refused_at_registration() -> None:
    with pytest.raises(TypeError, match="a filter must be callable, got str"):
        Router().on_message("hello")


def test_a_handler_must_be_callable() -> None:
    with pytest.raises(TypeError, match="a handler must be callable, got int"):
        Router().on_message()(42)  # type: ignore[type-var]


def test_command_needs_a_name() -> None:
    with pytest.raises(ValueError, match="at least one name"):
        Router().command()


def test_handlers_is_a_snapshot() -> None:
    router = Router()
    router.on_message()(handler_a)
    snapshot = router.handlers
    router.on_message()(handler_b)
    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1


def test_higher_priority_first_and_registration_order_within_a_priority() -> None:
    router = Router()
    router.on_message()(handler_a)
    router.on_message(priority=5)(handler_b)
    router.on_message()(handler_c)
    router.on_message(priority=5)(handler_d)
    router.on_message(priority=-1)(handler_e)
    assert names(router.iter_handlers(UpdateKind.MESSAGE)) == [
        "handler_b",
        "handler_d",
        "handler_a",
        "handler_c",
        "handler_e",
    ]


def test_own_handlers_first_then_sub_routers_depth_first_in_inclusion_order() -> None:
    root, first, nested, second = Router("root"), Router("first"), Router("nested"), Router("second")
    root.include_router(first)
    first.include_router(nested)
    root.include_router(second)
    second.on_message()(handler_e)
    nested.on_message()(handler_c)
    first.on_message()(handler_b)
    # priority orders handlers inside one router, never across routers
    first.on_message(priority=100)(handler_d)
    root.on_message()(handler_a)
    assert names(root.iter_handlers(UpdateKind.MESSAGE)) == [
        "handler_a",
        "handler_d",
        "handler_b",
        "handler_c",
        "handler_e",
    ]


def test_iter_handlers_only_yields_the_kind_asked_for_plus_on_update() -> None:
    router = Router()
    router.on_message()(handler_a)
    router.on_callback_query()(handler_b)
    router.on_update()(handler_c)
    router.on_edited_message()(handler_d)
    assert names(router.iter_handlers(UpdateKind.MESSAGE)) == ["handler_a", "handler_c"]
    assert names(router.iter_handlers(UpdateKind.CALLBACK_QUERY)) == ["handler_b", "handler_c"]
    assert names(router.iter_handlers(UpdateKind("poll"))) == ["handler_c"]


def test_all_handlers_walks_the_tree() -> None:
    root, child = Router(), Router()
    root.include_router(child)
    child.on_callback_query()(handler_b)
    root.on_message()(handler_a)
    assert [h.callback for h in root.all_handlers()] == [handler_a, handler_b]


def test_middleware_chains_run_from_the_root_down_to_the_handler_router() -> None:
    root, child, leaf, sibling = Router("root"), Router("child"), Router("leaf"), Router("sibling")
    root.include_router(child)
    child.include_router(leaf)
    root.include_router(sibling)
    assert root.middleware(mw_root_1) is mw_root_1
    root.middleware(mw_root_2)
    child.middleware(mw_child)
    leaf.middleware(mw_leaf)
    sibling.middleware(mw_sibling)
    root.on_message()(handler_a)
    child.on_message()(handler_b)
    leaf.on_message()(handler_c)
    sibling.on_message()(handler_d)
    chains = {h.callback.__name__: chain for h, chain in root.iter_handlers(UpdateKind.MESSAGE)}
    assert chains == {
        "handler_a": (mw_root_1, mw_root_2),
        "handler_b": (mw_root_1, mw_root_2, mw_child),
        "handler_c": (mw_root_1, mw_root_2, mw_child, mw_leaf),
        "handler_d": (mw_root_1, mw_root_2, mw_sibling),
    }
    assert list(root.all_middlewares()) == [mw_root_1, mw_root_2, mw_child, mw_leaf, mw_sibling]


def test_a_middleware_added_after_include_still_wraps_the_sub_router() -> None:
    root, child = Router(), Router()
    root.include_router(child)
    child.on_message()(handler_a)
    root.middleware(mw_root_1)
    ((_, chain),) = root.iter_handlers(UpdateKind.MESSAGE)
    assert chain == (mw_root_1,)


def test_iterating_a_sub_router_alone_leaves_the_parents_middlewares_out() -> None:
    root, child = Router(), Router()
    root.include_router(child)
    root.middleware(mw_root_1)
    child.middleware(mw_child)
    child.on_message()(handler_a)
    ((_, chain),) = child.iter_handlers(UpdateKind.MESSAGE)
    assert chain == (mw_child,)


def test_a_middleware_must_be_callable() -> None:
    with pytest.raises(TypeError, match="a middleware must be callable"):
        Router().middleware("log")  # type: ignore[arg-type]


def test_remove_handler_reaches_into_sub_routers() -> None:
    root, child, grandchild = Router(), Router(), Router()
    root.include_router(child)
    child.include_router(grandchild)
    root.on_message()(handler_a)
    grandchild.on_message()(handler_b)
    grandchild.on_edited_message()(handler_b)
    grandchild.on_message()(handler_c)
    assert root.remove_handler(handler_b) is True
    assert [h.callback for h in root.all_handlers()] == [handler_a, handler_c]
    assert root.remove_handler(handler_b) is False


def test_remove_handler_also_removes_error_handlers() -> None:
    root, child = Router(), Router()
    root.include_router(child)

    @child.on_error(KeyError)
    async def on_key_error(error: Exception) -> None: ...

    assert root.remove_handler(on_key_error) is True
    assert list(root.iter_error_handlers()) == []


def test_remove_handler_on_a_sub_router_does_not_touch_its_parent() -> None:
    root, child = Router(), Router()
    root.include_router(child)
    root.on_message()(handler_a)
    assert child.remove_handler(handler_a) is False
    assert [h.callback for h in root.all_handlers()] == [handler_a]


def test_on_error_without_types_catches_any_exception() -> None:
    router = Router()

    @router.on_error()
    async def anything(error: Exception) -> None: ...

    (handler,) = router.iter_error_handlers()
    assert handler.exceptions == (Exception,)
    assert handler.matches(ValueError())
    assert not handler.matches(KeyboardInterrupt())


def test_on_error_matches_subclasses_of_the_given_types() -> None:
    router = Router()
    router.on_error(LookupError, TimeoutError)(handler_a)
    (handler,) = router.iter_error_handlers()
    assert handler.matches(KeyError("k"))
    assert handler.matches(TimeoutError())
    assert not handler.matches(ValueError())


@pytest.mark.parametrize("bad", [ValueError("not a class"), int, "KeyError", None])
def test_on_error_takes_exception_classes_only(bad: Any) -> None:
    with pytest.raises(TypeError, match="on_error\\(\\) takes exception classes"):
        Router().on_error(KeyError, bad)


def test_error_handlers_are_walked_root_first() -> None:
    root, child = Router(), Router()
    root.include_router(child)
    child.on_error(KeyError)(handler_b)
    root.on_error()(handler_a)
    assert [h.callback for h in root.iter_error_handlers()] == [handler_a, handler_b]


def test_skip_handler_is_an_exception_to_raise() -> None:
    assert issubclass(SkipHandler, Exception)


def test_declared_commands_collects_descriptions_across_the_tree() -> None:
    root, child = Router(), Router()
    root.include_router(child)
    root.command("start", description="Say hello")(handler_a)
    root.command("secret")(handler_b)  # no description: not in the menu
    child.command("/Weather", "w", description="Today's weather")(handler_c)
    child.command("start", description="A second start")(handler_d)
    assert root.declared_commands() == [
        ("start", "Say hello"),
        ("weather", "Today's weather"),
        ("w", "Today's weather"),
    ]


def test_the_first_description_of_a_name_wins_even_after_an_undescribed_one() -> None:
    router = Router()
    router.command("help")(handler_a)
    router.command("help", description="How it works")(handler_b)
    assert router.declared_commands() == [("help", "How it works")]


async def test_command_handler_hands_over_the_parsed_command() -> None:
    router = Router()
    router.command("start", "go")(handler_a)
    handler = only(router)
    assert handler.commands == (("start", None), ("go", None))
    kwargs = await accepts(handler, message("/go now"))
    assert kwargs is not None
    assert kwargs["command"].args == "now"
    assert await accepts(handler, message("/stop")) is None
    assert await accepts(handler, message("/start@otherbot")) is None


async def test_command_takes_extra_filters_and_ignore_mention() -> None:
    router = Router()
    router.command("start", filters=(f.group,), ignore_mention=True)(handler_a)
    handler = only(router)
    assert await accepts(handler, message("/start@otherbot", chat="group")) is not None
    assert await accepts(handler, message("/start", chat="private")) is None


@pytest.mark.parametrize(
    ("flags", "accepted"),
    [
        ({}, {"private", "group", "channel"}),
        ({"private": True}, {"private"}),
        ({"group": True}, {"group"}),
        ({"channel": True}, {"channel"}),
        ({"private": True, "group": True}, {"private", "group"}),
        ({"private": False}, {"group", "channel"}),
        ({"private": False, "channel": False}, {"group"}),
        ({"group": True, "channel": False}, {"group"}),
        ({"private": None, "group": None, "channel": None}, {"private", "group", "channel"}),
    ],
)
@pytest.mark.parametrize("decorator", ["on_message", "on_edited_message"])
async def test_chat_kind_flags(decorator: str, flags: dict[str, bool | None], accepted: set[str]) -> None:
    router = Router()
    getattr(router, decorator)(**flags)(handler_a)
    handler = only(router)
    got = {kind for kind in CHATS if await accepts(handler, message("x", kind)) is not None}
    assert got == accepted


async def test_chat_kind_flags_add_to_the_given_filters() -> None:
    router = Router()
    router.on_message(f.has_text, private=True)(handler_a)
    handler = only(router)
    assert await accepts(handler, message("x", "private")) is not None
    assert await accepts(handler, message(None, "private")) is None
    assert await accepts(handler, message("x", "group")) is None


async def test_callback_query_data_prefix_and_pattern() -> None:
    router = Router()
    router.on_callback_query(data="yes")(handler_a)
    router.on_callback_query(prefix="page:")(handler_b)
    router.on_callback_query(pattern=r"^town:(\w+)$")(handler_c)
    router.on_callback_query(f.regex("^page"), data="page:1", pattern=r"1$")(handler_d)
    by_name = {h.callback.__name__: h for h in router.handlers}

    assert await accepts(by_name["handler_a"], press("yes")) == {}
    assert await accepts(by_name["handler_a"], press("yes!")) is None
    assert await accepts(by_name["handler_b"], press("page:4")) == {}
    assert await accepts(by_name["handler_b"], press("pages")) is None
    kwargs = await accepts(by_name["handler_c"], press("town:rome"))
    assert kwargs is not None
    assert kwargs["match"].group(1) == "rome"
    assert await accepts(by_name["handler_d"], press("page:1")) is not None
    assert await accepts(by_name["handler_d"], press("page:2")) is None


DATA = {"message": "dup", "command": "C", "match": "M", "bot": "B", "update": "U"}


def test_kwargs_for_gives_only_the_named_parameters() -> None:
    async def wants(message: Any, command: Any, *, bot: Any) -> None: ...

    assert Handler(wants, UpdateKind.MESSAGE, ()).kwargs_for(DATA) == {"command": "C", "bot": "B"}


def test_kwargs_for_never_passes_the_event_parameter_by_name() -> None:
    async def event_only(message: Any) -> None: ...

    assert Handler(event_only, UpdateKind.MESSAGE, ()).kwargs_for(DATA) == {}


def test_kwargs_for_with_var_keyword_takes_everything() -> None:
    async def greedy(event: Any, **extra: Any) -> None: ...

    handler = Handler(greedy, UpdateKind.MESSAGE, ())
    given = dict(DATA)
    out = handler.kwargs_for(given)
    assert out == DATA
    assert out is not given


def test_kwargs_for_skips_var_positional_and_positional_only() -> None:
    def odd(event: Any, /, match: Any, *rest: Any, update: Any = None) -> None: ...

    assert Handler(odd, None, ()).kwargs_for(DATA) == {"match": "M", "update": "U"}


def test_kwargs_for_works_on_bound_methods_and_callable_objects() -> None:
    class Handlers:
        async def on_text(self, message: Any, match: Any) -> None: ...

        async def __call__(self, message: Any, bot: Any) -> None: ...

    obj = Handlers()
    assert Handler(obj.on_text, UpdateKind.MESSAGE, ()).kwargs_for(DATA) == {"match": "M"}
    assert Handler(obj, UpdateKind.MESSAGE, ()).kwargs_for(DATA) == {"bot": "B"}


def test_kwargs_for_falls_back_to_everything_when_there_is_no_signature() -> None:
    class Opaque:
        __signature__ = "not a signature"

        def __call__(self, *args: Any, **kwargs: Any) -> None: ...

    assert Handler(Opaque(), UpdateKind.MESSAGE, ()).kwargs_for(DATA) == DATA


def test_error_handler_kwargs_for() -> None:
    async def on_error(error: Exception, update: Any) -> None: ...

    async def on_any(error: Exception, **kw: Any) -> None: ...

    assert ErrorHandler(on_error, (Exception,)).kwargs_for({"update": 1, "bot": 2}) == {"update": 1}
    assert ErrorHandler(on_any, (Exception,)).kwargs_for({"update": 1, "bot": 2}) == {"update": 1, "bot": 2}


def test_handler_name_and_is_async() -> None:
    def sync_handler(event: Any) -> None: ...

    class AsyncCallable:
        async def __call__(self, event: Any) -> None: ...

    assert Handler(handler_a, None, ()).is_async
    assert not Handler(sync_handler, None, ()).is_async
    assert Handler(AsyncCallable(), None, ()).is_async
    assert ErrorHandler(handler_a, (Exception,)).is_async
    assert Handler(sync_handler, None, ()).name.endswith("sync_handler")
    assert not is_async(lambda event: None)
