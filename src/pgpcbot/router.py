"""Routers: the handlers of a bot, with their filters, middlewares and error handlers.

`~pgpcbot.Bot` and `~pgpcbot.SyncBot` are routers, and a router can include other routers. To
split a bot into modules, give each module its own `~pgpcbot.Router` and include them all into
the bot with `~pgpcbot.Router.include_router`.

For each update, a router tries its own handlers first: higher ``priority`` first, then in the
order they were registered. Then come the included routers, in the order they were included,
each with the routers it includes (depth first). The first handler whose filters all match gets
the update. Raise `~pgpcbot.SkipHandler` inside it to pass the update on to the next one.

`~pgpcbot.router.Handler` and `~pgpcbot.router.ErrorHandler` are the records the decorators
store. `~pgpcbot.Router.iter_handlers` and the other readers give them back.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .filters import Filter, as_filter, callback_data, command, regex
from .filters import channel as _channel
from .filters import group as _group
from .filters import private as _private
from .models import UpdateKind

__all__ = ["ErrorHandler", "Handler", "Router", "SkipHandler"]

F = TypeVar("F", bound=Callable[..., Any])


class SkipHandler(Exception):  # noqa: N818 - it is a signal, not an error
    """Raised by a handler to pass the update on to the next matching handler.

    The next handler is tried as if this one's filters had not matched. When no handler is
    left, the update counts as handled. A middleware can raise it too.

    Raised by an error handler, it passes the error on to the next error handler whose types
    match. When no error handler is left, the update fails, as if no error handler had taken
    the error.

    A filter must not raise it: there it is an error like any other, which goes to the error
    handlers, and no handler runs. A filter that does not match returns ``False``.

    Example:
        A guard that deletes spam and lets everything else through::

            @bot.on_message(priority=10)
            async def spam_guard(message):
                if not looks_like_spam(message.text):
                    raise SkipHandler  # not spam: the next handler gets it
                await message.delete()
    """


@dataclass(eq=False, slots=True)
class Handler:
    """One registered handler: the function, the kind of update it takes and its filters.

    The decorators of `~pgpcbot.Router` make these, and `~pgpcbot.Router.handlers`,
    `~pgpcbot.Router.iter_handlers` and `~pgpcbot.Router.all_handlers` give them back. There
    is no need to make one by hand.

    Attributes:
        callback: The decorated function, as it was given.
        kind: The kind of update it takes, or ``None`` for every kind
            (`~pgpcbot.Router.on_update`).
        filters: The filters that must all match, each wrapped in a
            `~pgpcbot.filters.Filter`. The ones that the decorator's own arguments add (the
            command names, ``private=True``, ``prefix=...``) come first.
        priority: Higher numbers are tried first within the router.
        commands: For a `~pgpcbot.Router.command` handler, a ``(name, description)`` pair
            for each name, lowercased and without ``/``, with ``None`` when there is no
            description. Empty for the other handlers.
    """

    callback: Callable[..., Any]
    kind: UpdateKind | None
    filters: tuple[Filter, ...]
    priority: int = 0
    # for the command menu, see Bot.sync_commands
    commands: tuple[tuple[str, str | None], ...] = ()
    _params: frozenset[str] = field(default=frozenset(), repr=False)
    _takes_kwargs: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._params, self._takes_kwargs = _signature_of(self.callback)

    @property
    def is_async(self) -> bool:
        """``True`` when the callback is ``async``.

        That is an ``async def`` function, or an object whose ``__call__`` is ``async def``.
        A `~pgpcbot.Bot` refuses to start with a handler that is not async, a
        `~pgpcbot.SyncBot` with one that is.
        """
        return is_async(self.callback)

    @property
    def name(self) -> str:
        """The callback's ``__qualname__``, or its ``repr()`` when it has none.

        Error messages name the handler with it.
        """
        return getattr(self.callback, "__qualname__", repr(self.callback))

    def kwargs_for(self, data: dict[str, Any]) -> dict[str, Any]:
        """Picks out of ``data`` the extra arguments the callback asks for.

        The callback's first parameter takes the event. Each other parameter that can be passed
        by keyword is filled by name, when ``data`` has that key. A callback with ``**kwargs``,
        or one whose signature cannot be read, gets all of ``data``.

        Args:
            data: What the dispatch knows about the update: ``bot``, ``update``, and what the
                filters and the middlewares added (``command``, ``match``, ...).

        Returns:
            The keyword arguments to call the callback with.
        """
        if self._takes_kwargs:
            return dict(data)
        return {k: v for k, v in data.items() if k in self._params}


@dataclass(eq=False, slots=True)
class ErrorHandler:
    """One registered error handler: the function and the exception types it takes.

    `~pgpcbot.Router.on_error` makes these, and `~pgpcbot.Router.iter_error_handlers` gives
    them back. There is no need to make one by hand.

    Attributes:
        callback: The decorated function, as it was given.
        exceptions: The exception classes it takes. ``(Exception,)`` when
            `~pgpcbot.Router.on_error` was given none.
    """

    callback: Callable[..., Any]
    exceptions: tuple[type[BaseException], ...]
    _params: frozenset[str] = field(default=frozenset(), repr=False)
    _takes_kwargs: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._params, self._takes_kwargs = _signature_of(self.callback)

    @property
    def is_async(self) -> bool:
        """``True`` when the callback is ``async``.

        That is an ``async def`` function, or an object whose ``__call__`` is ``async def``.
        """
        return is_async(self.callback)

    def matches(self, error: BaseException) -> bool:
        """Tells whether this error handler takes ``error``.

        Args:
            error: The exception raised while the update was processed.

        Returns:
            ``True`` when ``error`` is an instance of one of `exceptions`, subclasses included.
        """
        return isinstance(error, self.exceptions)

    def kwargs_for(self, data: dict[str, Any]) -> dict[str, Any]:
        """Picks out of ``data`` the extra arguments the callback asks for.

        The callback's first parameter takes the error. Each other parameter that can be
        passed by keyword is filled by name, when ``data`` has that key. A callback with
        ``**kwargs``, or one whose signature cannot be read, gets all of ``data``.

        Args:
            data: ``bot``, ``update`` and ``error``.

        Returns:
            The keyword arguments to call the callback with.
        """
        if self._takes_kwargs:
            return dict(data)
        return {k: v for k, v in data.items() if k in self._params}


def is_async(func: Callable[..., Any]) -> bool:
    # a callable object with an async __call__ counts too
    return inspect.iscoroutinefunction(func) or (
        not inspect.isroutine(func) and inspect.iscoroutinefunction(type(func).__call__)
    )


def _signature_of(func: Callable[..., Any]) -> tuple[frozenset[str], bool]:
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return frozenset(), True
    # the first positional parameter is the event itself, the rest are asked for by name
    named = [p for p in params[1:] if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    return frozenset(p.name for p in named), any(p.kind == p.VAR_KEYWORD for p in params)


Middleware = Callable[..., Any]


class Router:
    """A set of handlers, with their middlewares and error handlers, and sub-routers.

    `Bot` and `SyncBot` are routers, so every decorator here works on a bot. A separate
    `Router` splits a bot into modules: register handlers on it, then include it into the bot
    with `include_router`.

    For each update, a router tries its own handlers first: higher ``priority`` first, then in
    the order they were registered. Then come the included routers, in the order they were
    included, each with the routers it includes (depth first). Only the handlers for the
    update's kind are tried, along with those registered with `on_update`. The first one whose
    filters all match gets the update, and the others are not tried unless it raises
    `SkipHandler`. When no handler matches, nothing happens and the update counts as handled.

    A handler's first parameter gets the event (a `Message`, a `CallbackQuery`, ...). The
    other parameters are filled by name:

    - ``bot``: the bot running the handler;
    - ``update``: the whole `Update`;
    - ``command``: the `ParsedCommand`, for a command;
    - ``match``: the :class:`re.Match` of a regex filter;
    - any key that a filter or a middleware added.

    A ``**kwargs`` parameter gets all of them. The first parameter must then have another
    name: ``async def log(update, **kwargs)`` in an `on_update` handler gets ``update`` twice,
    and the call fails with :exc:`TypeError`. A parameter that nothing fills keeps its
    default; without one the call raises :exc:`TypeError`, which goes to the error handlers.

    A `Bot` runs ``async def`` handlers, error handlers and middlewares, and a `SyncBot` plain
    functions, with no ``async`` filters. The bot checks this when it starts and raises
    `ConfigurationError` on a mismatch.

    Attributes:
        name (str): The router's name, shown in error messages and in ``repr()``.

    Example:
        A module with its own router, included into the bot::

            # shop.py
            router = Router(name="shop")

            @router.command("buy", description="Buy something")
            async def buy(message, command):
                await message.reply(f"{command.args} is in your cart")

            # main.py
            import shop

            bot = Bot()
            bot.include_router(shop.router)
            bot.run()
    """

    def __init__(self, name: str | None = None) -> None:
        """Makes an empty router.

        Args:
            name: A name for error messages and ``repr()``, such as ``"admin"``. ``None`` or
                ``""`` gives ``"router-"`` followed by the object's ``id()`` in hex.
        """
        self.name = name or f"router-{id(self):x}"
        self._handlers: list[Handler] = []
        self._error_handlers: list[ErrorHandler] = []
        self._middlewares: list[Middleware] = []
        self._children: list[Router] = []
        self._parent: Router | None = None

    def __repr__(self) -> str:
        return f"<Router {self.name} handlers={len(self._handlers)} children={len(self._children)}>"

    # --- structure ---

    def include_router(self, router: Router) -> Router:
        """Adds a sub-router, whose handlers are tried after this router's own.

        They also come after the handlers of the routers included before it. The middlewares
        of this router wrap the handlers of ``router`` too, and run before its own. A router
        can be included only once.

        Args:
            router: The router to include.

        Returns:
            ``router`` itself, so a router can be made and included in one line.

        Raises:
            ValueError: When ``router`` is this router, is already included somewhere, or
                contains this router (that would make a loop).
        """
        if router is self:
            raise ValueError("a router cannot include itself")
        if router._parent is not None:
            raise ValueError(f"{router.name} is already included in {router._parent.name}")
        node: Router | None = self
        while node is not None:
            if node is router:
                raise ValueError(f"including {router.name} here would make a loop")
            node = node._parent
        router._parent = self
        self._children.append(router)
        return router

    def include_routers(self, *routers: Router) -> None:
        """Adds several sub-routers, in order, as `include_router` does.

        Args:
            *routers: The routers to include.

        Raises:
            ValueError: As for `include_router`. The routers before the one that failed stay
                included.
        """
        for router in routers:
            self.include_router(router)

    # --- registration ---

    def _add(
        self,
        kind: UpdateKind | None,
        filters: tuple[Any, ...],
        priority: int,
        commands: tuple[tuple[str, str | None], ...] = (),
    ) -> Callable[[F], F]:
        checked = tuple(as_filter(f) for f in filters)

        def decorator(func: F) -> F:
            if not callable(func):
                raise TypeError(f"a handler must be callable, got {type(func).__name__}")
            self._handlers.append(Handler(func, kind, checked, priority, commands))
            # stable: same priority keeps registration order
            self._handlers.sort(key=lambda h: -h.priority)
            return func

        return decorator

    def on_message(
        self,
        *filters: Any,
        private: bool | None = None,
        group: bool | None = None,
        channel: bool | None = None,
        priority: int = 0,
    ) -> Callable[[F], F]:
        """Registers a handler for new messages.

        The handler gets a `Message` (a `SyncMessage` on a `SyncBot`). Commands are messages
        too: register the `command` handlers first, or a handler here with no filters takes
        them.

        Args:
            *filters: Filters that must all match: the ready-made ones in `pgpcbot.filters`,
                `~pgpcbot.filters.Filter` objects, or plain functions that take the message.
            private: ``True`` takes only private chats, ``False`` anything but private chats,
                ``None`` (the default) does not look at the chat. Several ``True`` flags take
                either kind: ``private=True, group=True``.
            group: The same, for groups.
            channel: The same, for channels.
            priority: Handlers with a higher number are tried first within this router. The
                same number keeps the order of registration.

        Returns:
            A decorator that registers the function and gives it back unchanged.

        Example:
            An echo for private chats::

                @bot.on_message(f.has_text, private=True)
                async def echo(message):
                    await message.reply(message.text)
        """
        return self._add(UpdateKind.MESSAGE, _with_chat_kind(filters, private, group, channel), priority)

    def on_edited_message(
        self,
        *filters: Any,
        private: bool | None = None,
        group: bool | None = None,
        channel: bool | None = None,
        priority: int = 0,
    ) -> Callable[[F], F]:
        """Registers a handler for edits: somebody edited a message the bot had received.

        The handler gets the `Message` as it is now, with the new text (a `SyncMessage` on a
        `SyncBot`). The arguments work as in `on_message`.

        Args:
            *filters: Filters that must all match.
            private: ``True`` takes only private chats, ``False`` anything but private chats,
                ``None`` (the default) does not look at the chat.
            group: The same, for groups.
            channel: The same, for channels.
            priority: Handlers with a higher number are tried first within this router.

        Returns:
            A decorator that registers the function and gives it back unchanged.
        """
        return self._add(UpdateKind.EDITED_MESSAGE, _with_chat_kind(filters, private, group, channel), priority)

    def command(
        self,
        *names: str,
        description: str | None = None,
        filters: tuple[Any, ...] = (),
        ignore_mention: bool = False,
        priority: int = 0,
    ) -> Callable[[F], F]:
        """Registers a handler for one or more commands, ``/name`` and ``/name@yourbot``.

        The handler gets the `Message` (a `SyncMessage` on a `SyncBot`) and, if it has a
        parameter called ``command``, the `ParsedCommand` with the arguments. A command
        addressed to another bot, such as ``/start@otherbot``, is ignored unless
        ``ignore_mention`` is ``True``. Only new messages count: a message edited into a
        command does not run it.

        Args:
            *names: The command names, each 1 to 32 letters, digits or ``_``, with or without
                ``/``. Case does not matter. The names are not checked here: one that breaks
                this rule, such as ``"my-command"``, never matches.
            description: The line shown next to the command in the menu the app opens when
                someone types ``/``, 1 to 256 characters. `Bot.sync_commands` publishes the
                commands that have one (see `declared_commands`), and checks the names and
                descriptions then; ``None`` leaves the command out of the menu.
            filters: More filters that must match, as a tuple: ``filters=(f.private,)``.
            ignore_mention: ``True`` also takes the commands addressed to another bot.
            priority: Handlers with a higher number are tried first within this router.

        Returns:
            A decorator that registers the function and gives it back unchanged.

        Raises:
            ValueError: When no name is given. To take any command, use
                ``on_message(f.command())``.

        Example:
            ``/weather milan`` answers for Milan, ``/weather`` alone for Rome::

                @bot.command("weather", description="Forecast for a town")
                async def weather(message, command):
                    town = command.args or "Rome"
                    await message.reply(f"{town}: sun.")
        """
        if not names:
            raise ValueError("command() needs at least one name; use on_message(filters.command()) for any command")
        listed = tuple((n.lstrip("/").lower(), description) for n in names)
        return self._add(
            UpdateKind.MESSAGE,
            (command(*names, ignore_mention=ignore_mention), *filters),
            priority,
            commands=listed,
        )

    def on_callback_query(
        self,
        *filters: Any,
        data: str | None = None,
        prefix: str | None = None,
        pattern: str | None = None,
        priority: int = 0,
    ) -> Callable[[F], F]:
        """Registers a handler for presses on callback buttons.

        The handler gets a `CallbackQuery` (a `SyncCallbackQuery` on a `SyncBot`). Answer it
        with `CallbackQuery.answer` within 60 seconds. A press that nothing answered gets an
        empty answer once the handlers are done, even when they failed, so the button stops
        spinning (unless the bot was made with ``auto_answer_callbacks=False``).

        Args:
            *filters: Filters that must all match.
            data: Takes only the presses whose data is exactly this, such as
                ``"menu:settings"``.
            prefix: Takes only the presses whose data starts with this, such as ``"vote:"``.
                Given with ``data``, either one is enough.
            pattern: A regular expression searched in the data (:func:`re.search`); the
                handler can ask for the ``match``. Given with ``data`` or ``prefix``, both
                must match.
            priority: Handlers with a higher number are tried first within this router.

        Returns:
            A decorator that registers the function and gives it back unchanged.

        Example:
            One handler for every ``vote:`` button::

                @bot.on_callback_query(prefix="vote:")
                async def vote(query):
                    choice = query.data.removeprefix("vote:")
                    await query.answer(f"You voted {choice}")
        """
        extra: list[Any] = []
        if data is not None or prefix is not None:
            extra.append(callback_data(*(() if data is None else (data,)), prefix=prefix))
        if pattern is not None:
            extra.append(regex(pattern))
        return self._add(UpdateKind.CALLBACK_QUERY, (*extra, *filters), priority)

    def on_deleted_messages(self, *filters: Any, priority: int = 0) -> Callable[[F], F]:
        """Registers a handler for deletions: messages were deleted for everyone.

        The handler gets a `DeletedMessages`, with the chat and the IDs of the messages.

        Args:
            *filters: Filters that must all match. Only the ones that look at the chat
                (``f.private``, ``f.chat(...)``) can match here: a deletion has no text and no
                sender.
            priority: Handlers with a higher number are tried first within this router.

        Returns:
            A decorator that registers the function and gives it back unchanged.
        """
        return self._add(UpdateKind.DELETED_MESSAGES, filters, priority)

    def on_chat_member(self, *filters: Any, priority: int = 0) -> Callable[[F], F]:
        """Registers a handler for changes to the bot's own place in a chat.

        The bot was added, promoted, restricted or removed, or a person blocked or unblocked
        it. The handler gets a `ChatMemberUpdate`.

        Args:
            *filters: Filters that must all match.
            priority: Handlers with a higher number are tried first within this router.

        Returns:
            A decorator that registers the function and gives it back unchanged.

        Example:
            A thank-you when the bot is added::

                @bot.on_chat_member()
                async def membership(change):
                    if change.joined:
                        await bot.send_message(change.chat, "Thanks for adding me.")
        """
        return self._add(UpdateKind.MY_CHAT_MEMBER, filters, priority)

    def on_update(self, *filters: Any, priority: int = 0) -> Callable[[F], F]:
        """Registers a handler for every update, of any kind.

        The handler gets the `Update` itself, and so do its filters: the ones that look at the
        chat or at the sender (``f.private``, ``f.chat(...)``, ``f.user(...)``) work, the
        others (text, media, button data) never match. It takes its turn like any other
        handler: registered before the others, it gets every update unless it raises
        `SkipHandler`.

        Args:
            *filters: Filters that must all match. They are called with the `Update`.
            priority: Handlers with a higher number are tried first within this router.

        Returns:
            A decorator that registers the function and gives it back unchanged.
        """
        return self._add(None, filters, priority)

    def on_error(self, *exceptions: type[BaseException]) -> Callable[[F], F]:
        """Registers an error handler, for errors raised while an update is processed.

        It takes the errors raised by filters, middlewares and handlers, and the ones raised
        while an encrypted message is opened (`DecryptionError`, `SignatureError`, and
        `KeyChangedError` with ``pin_keys=True``), in which case no handler runs. The callback
        gets the error first, and can ask for ``bot``, ``update`` and ``error`` by name; what
        filters and middlewares added does not reach it. With ``**kwargs``, the first
        parameter must not be called ``error``, or the call fails with :exc:`TypeError`.

        Error handlers are tried in order: this router's first, in the order they were
        registered, then those of the included routers, depth first. The first one whose
        types match runs; raise `SkipHandler` in it to pass the error on to the next one. If it
        returns normally, the update counts as handled. If it raises, or no error handler
        takes the error, the update fails: a running bot keeps it in its failed list with the
        error (`Bot.failed_updates`), and `Bot.process_update` raises the error.

        Args:
            *exceptions: The exception classes to take, subclasses included. None at all
                takes any `Exception`. Errors that are not an `Exception`, such as
                ``KeyboardInterrupt``, never reach an error handler.

        Returns:
            A decorator that registers the function and gives it back unchanged.

        Raises:
            TypeError: When one of ``exceptions`` is not an exception class.

        Example:
            A last resort that logs the error, after which the update counts as handled::

                @bot.on_error()
                async def everything_else(error, update):
                    log.error("update %s failed", update.update_id, exc_info=error)

        Note:
            Error handlers are not limited to their own router: the bot tries all of them, in
            the order above, for an error raised anywhere.
        """
        types = exceptions or (Exception,)
        given: tuple[object, ...] = types
        for t in given:
            if not (isinstance(t, type) and issubclass(t, BaseException)):
                raise TypeError(f"on_error() takes exception classes, got {t!r}")

        def decorator(func: F) -> F:
            self._error_handlers.append(ErrorHandler(func, types))
            return func

        return decorator

    def middleware(self, func: Middleware) -> Middleware:
        """Adds a middleware around every handler of this router and of its sub-routers.

        A middleware is called as ``mw(handler, event, data)``. ``handler`` runs the rest of
        the chain (the inner middlewares, then the handler), ``event`` is what the handler
        gets, and ``data`` is the dict of extra handler arguments. Call
        ``await handler(event, data)`` to go on, and return what it returns. Not calling it
        stops the update there: no other handler is tried. Raising `SkipHandler` passes the
        update on to the next matching handler. A key added to ``data`` reaches the handler,
        which can ask for it by name.

        Middlewares run only around a handler whose filters matched, from the outside in: the
        outer routers' first, then this router's, in the order they were added. On a
        `SyncBot` a middleware is a plain function that returns ``handler(event, data)``.

        Args:
            func: The middleware.

        Returns:
            ``func`` itself, so this method works as a decorator.

        Raises:
            TypeError: When ``func`` is not callable.

        Example:
            Loading a profile that handlers can ask for::

                @bot.middleware
                async def load_profile(handler, event, data):
                    data["profile"] = await db.load(event.from_user.id) if event.from_user else None
                    return await handler(event, data)

                @bot.on_message()
                async def hello(message, profile): ...
        """
        if not callable(func):
            raise TypeError("a middleware must be callable")
        self._middlewares.append(func)
        return func

    def remove_handler(self, callback: Callable[..., Any]) -> bool:
        """Removes every handler and error handler of ``callback``, here and in the sub-routers.

        The decorators give the function back unchanged, so pass the decorated function.
        Middlewares are not touched.

        Args:
            callback: The function that was registered.

        Returns:
            ``True`` when something was removed.
        """
        removed = False
        for router in self._walk():
            before = len(router._handlers) + len(router._error_handlers)
            router._handlers = [h for h in router._handlers if h.callback is not callback]
            router._error_handlers = [h for h in router._error_handlers if h.callback is not callback]
            removed = removed or before != len(router._handlers) + len(router._error_handlers)
        return removed

    # --- reading back ---

    @property
    def handlers(self) -> tuple[Handler, ...]:
        """This router's own handlers, in the order they are tried.

        The handlers of included routers are not in it; `all_handlers` walks the whole tree.
        """
        return tuple(self._handlers)

    def _walk(self) -> Iterator[Router]:
        yield self
        for child in self._children:
            yield from child._walk()

    def iter_handlers(self, kind: UpdateKind) -> Iterator[tuple[Handler, tuple[Middleware, ...]]]:
        """Yields the handlers that can take an update of ``kind``, in the order they are tried.

        The bot walks this for each update. The handlers registered with `on_update` come up
        for every kind.

        Args:
            kind: The kind of update.

        Yields:
            Pairs of a `~pgpcbot.router.Handler` and the tuple of middlewares around it,
            outermost first.
        """
        yield from self._iter(kind, ())

    def _iter(
        self, kind: UpdateKind, outer: tuple[Middleware, ...]
    ) -> Iterator[tuple[Handler, tuple[Middleware, ...]]]:
        chain = (*outer, *self._middlewares)
        for handler in self._handlers:
            if handler.kind is None or handler.kind == kind:
                yield handler, chain
        for child in self._children:
            yield from child._iter(kind, chain)

    def iter_error_handlers(self) -> Iterator[ErrorHandler]:
        """Yields every error handler of this router and below, in the order they are tried.

        This router's come first, in the order they were registered, then those of each
        included router, depth first.

        Yields:
            The `~pgpcbot.router.ErrorHandler` records.
        """
        for router in self._walk():
            yield from router._error_handlers

    def all_handlers(self) -> Iterator[Handler]:
        """Yields every handler of this router and below, whatever kind of update it takes.

        They come in the order they are tried: this router's first, then those of each
        included router, depth first.

        Yields:
            The `~pgpcbot.router.Handler` records.
        """
        for router in self._walk():
            yield from router._handlers

    def all_middlewares(self) -> Iterator[Middleware]:
        """Yields every middleware of this router and below.

        This router's come first, in the order they were added, then those of each included
        router, depth first.

        Yields:
            The middleware functions.
        """
        for router in self._walk():
            yield from router._middlewares

    def declared_commands(self) -> list[tuple[str, str]]:
        """Lists the commands registered with a description, in this router and below.

        This is the menu that `Bot.sync_commands` publishes. Commands without a description
        are left out. When a name has several descriptions, the first one wins.

        Returns:
            ``(name, description)`` pairs in the order the handlers are tried, each name
            lowercased and without ``/``.
        """
        seen: dict[str, str] = {}
        for handler in self.all_handlers():
            for name, description in handler.commands:
                if description and name not in seen:
                    seen[name] = description
        return list(seen.items())


def _with_chat_kind(
    filters: tuple[Any, ...], private: bool | None, group: bool | None, channel: bool | None
) -> tuple[Any, ...]:
    wanted = [f for flag, f in ((private, _private), (group, _group), (channel, _channel)) if flag]
    unwanted = [f for flag, f in ((private, _private), (group, _group), (channel, _channel)) if flag is False]
    extra: list[Any] = []
    if wanted:
        combined = wanted[0]
        for f in wanted[1:]:
            combined = combined | f
        extra.append(combined)
    extra.extend(~f for f in unwanted)
    return (*extra, *filters)
