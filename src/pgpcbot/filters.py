"""Filters decide which handler gets an update.

The filters go in the decorator, and all of them must match. They run in order, and the
first one that does not match stops the others::

    from pgpcbot import filters as f

    @bot.on_message(f.private, f.has_text)
    async def chat(message): ...

A filter is called with the event the handler would get (a `~pgpcbot.Message`, a
`~pgpcbot.CallbackQuery`, ...) and answers whether it matches. Any function that takes the
event and returns ``True`` or ``False`` is a filter, lambdas included. It can be ``async``,
except on a `~pgpcbot.SyncBot`. A function with two positional parameters also gets a
`~pgpcbot.filters.FilterContext`, with the bot's own handle.

A filter can answer with a ``dict`` instead of ``True``. That is a match, and the handler can
ask for each item of the dict by name: this is how `command` hands over ``command`` and
`regex` hands over ``match``. ``False``, ``None`` and any other false value mean no match.

`Filter` objects combine with ``&`` (and), ``|`` (or) and ``~`` (not). A plain function
combines with a `Filter` on the other side, so ``f.private & my_function`` works.

Ready-made filters, to use as they are:

- `private`, `group`, `channel`: the kind of chat.
- `has_text`: the message has text.
- `has_media`: the message has attachments.
- `encrypted`: the message came end-to-end encrypted.
- `signed`: encrypted, with a valid signature from the sender's key.
- `reply`: the message answers another one.
- `mentioned`: the text mentions the bot's ``@handle``.

Filters you make with your own values:

- `text` (``f.text("yes", "no")``): the whole text is one of the values.
- `regex` (``f.regex(r"^order [0-9]+$")``): the text, or a button's data, contains a match.
- `command` (``f.command("start", "help")``): one of these commands; with no names, any.
- `user` (``f.user(42, "@anna")``): from one of these people.
- `chat` (``f.chat(7781, "@news")``): in one of these chats.
- `callback_data` (``f.callback_data(prefix="vote:")``): a button press with this data.

`parse_command` reads a command the way the server does.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ._validate import JS_SPACE
from .models import CallbackQueryBase, ChatKind, ChatRef, MessageBase, SignatureStatus, User

__all__ = [
    "Filter",
    "ParsedCommand",
    "callback_data",
    "channel",
    "chat",
    "command",
    "encrypted",
    "group",
    "has_media",
    "has_text",
    "mentioned",
    "parse_command",
    "private",
    "regex",
    "reply",
    "signed",
    "text",
    "user",
]

FilterResult = bool | dict[str, Any] | None
FilterFunc = Callable[..., FilterResult | Awaitable[FilterResult]]

# mirrors the server's own rule for "/cmd" and "/cmd@somebot"
# re.ASCII: unicode case folding would let a long s (U+017F) pass for an s, javascript's /i doesn't.
# that also narrows \s, so the end of the command is javascript's own set of spaces
_COMMAND_RE = re.compile(
    r"^/([a-z0-9_]{1,32})(?:@([A-Za-z0-9_]{5,32}))?(?=[" + re.escape(JS_SPACE) + "]|$)",
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """A command read from a message, such as ``/start@weatherbot rome``.

    A handler registered with `Router.command`, or with a `~pgpcbot.filters.command` filter,
    gets one when it has a parameter called ``command``. `~pgpcbot.filters.parse_command`
    makes one from any text.

    Attributes:
        name: The command name, lowercased, without ``/``: ``"start"``.
        args: Everything after the command, with the spaces at both ends removed: ``"rome"``.
            An empty string when nothing follows.
        mention: The handle after ``@``, as written: ``"weatherbot"``. ``None`` when there is
            none.
        token: The command exactly as written, with its ``@handle`` if it has one:
            ``"/start@weatherbot"``.

    Example:
        >>> from pgpcbot.filters import parse_command
        >>> command = parse_command("/Weather@WeatherBot rome  3")
        >>> command.name, command.mention, command.args
        ('weather', 'WeatherBot', 'rome  3')
        >>> command.argv
        ['rome', '3']
    """

    name: str
    args: str
    mention: str | None
    token: str

    @property
    def argv(self) -> list[str]:
        """`args` split on whitespace, ``["rome", "3"]`` for ``/weather rome 3``."""
        return self.args.split()


def parse_command(text: str | None) -> ParsedCommand | None:
    """Reads the command at the start of a text, by the same rule as the server.

    A command is ``/``, then 1 to 32 letters, digits or ``_``, then optionally ``@`` and a
    handle of 5 to 32 of the same characters, then whitespace or the end of the text. Only
    ASCII letters count, in any case. Nothing may come before the ``/``.

    Args:
        text: The text of a message. ``None`` and ``""`` are fine.

    Returns:
        The command, or ``None`` when the text does not start with one.

    Example:
        >>> from pgpcbot.filters import parse_command
        >>> parse_command("/start@WeatherBot rome")
        ParsedCommand(name='start', args='rome', mention='WeatherBot', token='/start@WeatherBot')
        >>> parse_command("/start")
        ParsedCommand(name='start', args='', mention=None, token='/start')
        >>> print(parse_command("hello /start"))
        None
    """
    if not text:
        return None
    m = _COMMAND_RE.match(text)
    if m is None:
        return None
    return ParsedCommand(
        name=m.group(1).lower(),
        args=text[m.end() :].strip(),
        mention=m.group(2),
        token=m.group(0),
    )


class Filter:
    """A condition on an event, which combines with others through operators.

    The ready-made filters of this module are `Filter` objects. Plain functions given to a
    decorator are wrapped for you; wrap one yourself to give it a name, or to use ``~`` on
    it. The operators make new filters:

    - ``a & b`` matches when both match. ``b`` is not called when ``a`` does not match. The
      values that both hand over go to the handler.
    - ``a | b`` matches when either matches. ``b`` is called only when ``a`` does not match,
      and the handler gets the values of the one that matched.
    - ``~a`` matches when ``a`` does not, and hands over nothing.

    One side of ``&`` and ``|`` can be a plain function. A combination with an ``async`` part
    is ``async`` itself.

    Example:
        Private messages from people who are not subscribers yet::

            async def is_subscriber(message):
                return await db.is_subscriber(message.from_user.id)

            @bot.on_message(f.private & ~f.Filter(is_subscriber))
            async def pitch(message):
                await message.reply("Subscribe to read more.")
    """

    __slots__ = ("_func", "_name", "_with_context")

    def __init__(self, func: FilterFunc, name: str | None = None) -> None:
        """Wraps ``func`` as a filter.

        Args:
            func: The condition. It takes the event, or the event and a `FilterContext` when
                it has two or more positional parameters (or ``*args``). It returns ``True``,
                ``False`` or a ``dict`` of values for the handler, and may be ``async``.
            name: The name shown by ``repr()`` and in error messages. ``None`` takes the
                function's ``__name__``, or ``"filter"`` when it has none.
        """
        self._func = func
        self._name = name or getattr(func, "__name__", "filter")
        self._with_context = _accepts_context(func)

    def __call__(self, event: Any, context: FilterContext) -> Any:
        """Runs the filter on ``event``.

        The bot does this for you; tests can call it directly.

        Args:
            event: The event a handler would get.
            context: What the filter can look at besides the event. A function that takes only
                the event does not get it.

        Returns:
            The function's answer, ``True``, ``False``, ``None`` or a ``dict``, or an awaitable
            of it when the function is ``async``.
        """
        return self._func(event, context) if self._with_context else self._func(event)

    def __and__(self, other: Any) -> Filter:
        return _And(self, as_filter(other))

    def __rand__(self, other: Any) -> Filter:
        return _And(as_filter(other), self)

    def __or__(self, other: Any) -> Filter:
        return _Or(self, as_filter(other))

    def __ror__(self, other: Any) -> Filter:
        return _Or(as_filter(other), self)

    def __invert__(self) -> Filter:
        return _Not(self)

    @property
    def is_async(self) -> bool:
        """``True`` when the function is ``async``, or for a combination when any part is.

        A `~pgpcbot.SyncBot` refuses to start with an ``async`` filter
        (`~pgpcbot.ConfigurationError`).
        """
        func = self._func
        return inspect.iscoroutinefunction(func) or (
            not inspect.isroutine(func) and inspect.iscoroutinefunction(type(func).__call__)
        )

    def __repr__(self) -> str:
        return f"<Filter {self._name}>"


@dataclass(frozen=True, slots=True)
class FilterContext:
    """What a filter can look at besides the event.

    The bot makes one for each update, and passes it to every filter function that takes two
    positional parameters.

    Attributes:
        bot_username: The bot's handle, without ``@``. ``None`` until the bot has read its own
            profile from the server, which it does before the first update.
        update: The whole `~pgpcbot.Update` being routed.
        bot: The `~pgpcbot.Bot` or `~pgpcbot.SyncBot` that runs the filter.

    Example:
        Messages that start with the bot's handle::

            def starts_with_my_name(message, context):
                handle = f"@{context.bot_username}".lower()
                return (message.text or "").lower().startswith(handle)

            @bot.on_message(starts_with_my_name)
            async def called(message): ...
    """

    bot_username: str | None
    update: Any
    bot: Any


def _accepts_context(func: Callable[..., Any]) -> bool:
    # plain lambdas take just the event, filters that need the bot's handle take two
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in params)


async def resolve(result: Any) -> FilterResult:
    if inspect.isawaitable(result):
        result = await result
    return result  # type: ignore[no-any-return]


def passed(result: FilterResult) -> bool:
    # a dict is a pass even when it is empty: it means "match, and nothing to hand over"
    return isinstance(result, dict) or bool(result)


def merge(result: FilterResult, into: dict[str, Any]) -> bool:
    """Folds one filter answer into the handler's extra arguments. False stops the match."""
    if isinstance(result, dict):
        into.update(result)
        return True
    return bool(result)


class _And(Filter):
    __slots__ = ("_left", "_right")

    def __init__(self, left: Filter, right: Filter) -> None:
        self._left, self._right = left, right
        super().__init__(self._run, f"({left._name} & {right._name})")

    def _run(self, event: Any, context: FilterContext) -> Any:
        left = self._left(event, context)
        if inspect.isawaitable(left):
            return self._finish_async(left, event, context)
        data: dict[str, Any] = {}
        if not merge(left, data):
            return False
        right = self._right(event, context)
        if inspect.isawaitable(right):
            return self._finish_right(right, data)
        if not merge(right, data):
            return False
        return data or True

    async def _finish_async(self, left: Any, event: Any, context: FilterContext) -> Any:
        data: dict[str, Any] = {}
        if not merge(await left, data):
            return False
        if not merge(await resolve(self._right(event, context)), data):
            return False
        return data or True

    async def _finish_right(self, right: Any, data: dict[str, Any]) -> Any:
        if not merge(await right, data):
            return False
        return data or True

    @property
    def is_async(self) -> bool:
        return self._left.is_async or self._right.is_async


class _Or(Filter):
    __slots__ = ("_left", "_right")

    def __init__(self, left: Filter, right: Filter) -> None:
        self._left, self._right = left, right
        super().__init__(self._run, f"({left._name} | {right._name})")

    def _run(self, event: Any, context: FilterContext) -> Any:
        left = self._left(event, context)
        if inspect.isawaitable(left):
            return self._finish_async(left, event, context)
        if passed(left):
            return left
        return self._right(event, context)

    async def _finish_async(self, left: Any, event: Any, context: FilterContext) -> Any:
        first = await left
        if passed(first):
            return first
        return await resolve(self._right(event, context))

    @property
    def is_async(self) -> bool:
        return self._left.is_async or self._right.is_async


class _Not(Filter):
    __slots__ = ("_inner",)

    def __init__(self, inner: Filter) -> None:
        self._inner = inner
        super().__init__(self._run, f"~{inner._name}")

    def _run(self, event: Any, context: FilterContext) -> Any:
        result = self._inner(event, context)
        if inspect.isawaitable(result):
            return self._finish_async(result)
        return not passed(result)

    async def _finish_async(self, result: Any) -> bool:
        return not passed(await result)

    @property
    def is_async(self) -> bool:
        return self._inner.is_async


def as_filter(value: Any) -> Filter:
    if isinstance(value, Filter):
        return value
    if callable(value):
        return Filter(value)
    raise TypeError(f"a filter must be callable, got {type(value).__name__}")


# --- the usual ones ---


def _chat_of(event: Any) -> ChatRef | None:
    chat = getattr(event, "chat", None)
    return chat if isinstance(chat, ChatRef) else None


def _kind(kind: ChatKind) -> Filter:
    return Filter(lambda event: (c := _chat_of(event)) is not None and c.kind == kind, kind.value)


private = _kind(ChatKind.PRIVATE)
"""Matches events in a private chat, between the bot and one person.

It works for every kind of update, since each one carries its chat. ``private=True`` in
`pgpcbot.Router.on_message` does the same.
"""
group = _kind(ChatKind.GROUP)
"""Matches events in a group. It works for every kind of update."""
channel = _kind(ChatKind.CHANNEL)
"""Matches events in a channel. It works for every kind of update."""

has_text = Filter(lambda event: bool(getattr(event, "text", None)), "has_text")
"""Matches a message that has text, decrypted when the message came encrypted.

A message with attachments and no text does not match, and neither does a button press.
"""
has_media = Filter(lambda event: bool(getattr(event, "media", None)), "has_media")
"""Matches a message with attachments, such as pictures, clips or files."""
encrypted = Filter(lambda event: isinstance(event, MessageBase) and event.encrypted, "encrypted")
"""Matches a message that came end-to-end encrypted.

The handler still gets it decrypted. Only messages match.
"""
reply = Filter(lambda event: isinstance(event, MessageBase) and event.reply_to is not None, "reply")
"""Matches a message that answers (quotes) another message."""
signed = Filter(
    lambda event: (
        isinstance(event, MessageBase)
        and event.verification is not None
        and event.verification.status == SignatureStatus.VALID
    ),
    "signed",
)
"""Matches an encrypted message with a valid signature from the sender's key.

The sender's key is the one the server lists for them in the chat. With
``verify_signatures="off"`` the bot checks no signature, so nothing matches.
"""


def text(*values: str, ignore_case: bool = False) -> Filter:
    """Makes a filter for messages whose whole text is one of ``values``.

    Spaces at both ends of the message are ignored. The values are compared as they are given.

    Args:
        *values: The texts to take, such as ``"yes", "no"``.
        ignore_case: ``True`` compares without case (:meth:`str.casefold`).

    Returns:
        A filter that matches a message whose trimmed text equals one of ``values``.

    Example:
        A yes or no answer, in any case::

            @bot.on_message(f.text("yes", "no", ignore_case=True))
            async def answer(message): ...
    """
    wanted = {v.casefold() if ignore_case else v for v in values}

    def check(event: Any) -> bool:
        body = getattr(event, "text", None)
        if not isinstance(body, str):
            return False
        return (body.strip().casefold() if ignore_case else body.strip()) in wanted

    return Filter(check, f"text{values!r}")


def regex(pattern: str | re.Pattern[str], flags: int = 0) -> Filter:
    """Makes a filter that searches the text of a message, or the data of a button press.

    The pattern can match anywhere in the text (:func:`re.search`): anchor it with ``^`` and
    ``$`` to match the whole text. The match is handed to the handler as ``match``.

    Args:
        pattern: A regular expression, as a string or compiled.
        flags: :mod:`re` flags, such as ``re.IGNORECASE``, for a ``pattern`` given as a
            string. Ignored when ``pattern`` is compiled.

    Returns:
        A filter that matches when the pattern is found, and hands over the :class:`re.Match`
        as ``match``.

    Example:
        An order number, read from the match::

            @bot.on_message(f.regex(r"^order ([0-9]+)$"))
            async def order(message, match):
                await message.reply(f"Order {match[1]}: shipped")
    """
    compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def check(event: Any) -> FilterResult:
        if isinstance(event, CallbackQueryBase):
            subject: str | None = event.data
        else:
            subject = getattr(event, "text", None)
        if not isinstance(subject, str):
            return False
        m = compiled.search(subject)
        return {"match": m} if m else False

    return Filter(check, f"regex({compiled.pattern!r})")


def command(*names: str, ignore_mention: bool = False) -> Filter:
    """Makes a filter for the commands ``/name`` and ``/name@yourbot``.

    The command must start the text and be followed by whitespace or nothing (see
    `parse_command`). A command addressed to another bot, such as ``/start@otherbot``, does
    not match unless ``ignore_mention`` is ``True``. `pgpcbot.Router.command` registers its
    handlers with this filter; use the filter directly to combine it with others.

    Args:
        *names: The command names, each 1 to 32 letters, digits or ``_``, with or without
            ``/``. Case does not matter. With no names, any command matches.
        ignore_mention: ``True`` also takes the commands addressed to another bot.

    Returns:
        A filter that matches those commands, and hands over the `~pgpcbot.ParsedCommand`
        as ``command``.

    Example:
        An answer to every command that no handler before it took::

            @bot.on_message(f.command(), private=True)
            async def unknown(message, command):
                await message.reply(f"I don't know /{command.name}")
    """
    wanted = {n.lstrip("/").lower() for n in names}

    def check(event: Any, context: FilterContext) -> FilterResult:
        parsed = parse_command(getattr(event, "text", None))
        if parsed is None or (wanted and parsed.name not in wanted):
            return False
        if parsed.mention and not ignore_mention:
            me = (context.bot_username or "").lower()
            if parsed.mention.lower() != me:
                return False
        return {"command": parsed}

    return Filter(check, f"command{tuple(sorted(wanted))!r}")


def _mentions_bot(event: Any, context: FilterContext) -> bool:
    body = getattr(event, "text", None)
    handle = context.bot_username
    if not isinstance(body, str) or not handle:
        return False
    return re.search(rf"(^|[^a-z0-9_])@{re.escape(handle)}(?![a-z0-9_])", body, re.IGNORECASE) is not None


mentioned = Filter(_mentions_bot, "mentioned")
"""Matches a message whose text mentions the bot's ``@handle``.

The rule is the one the server uses in groups: the ``@handle`` can be anywhere in the text, in
any case, but not glued to a letter, a digit or ``_`` on either side.
"""


def _ids_and_names(values: Iterable[int | str]) -> tuple[set[int], set[str]]:
    ids: set[int] = set()
    names: set[str] = set()
    for value in values:
        if isinstance(value, bool):
            raise TypeError("a user or chat must be an id or a @handle, not a bool")
        if isinstance(value, int):
            ids.add(value)
        else:
            names.add(str(value).lstrip("@").lower())
    return ids, names


def user(*who: int | str) -> Filter:
    """Makes a filter for messages and button presses from one of these people.

    Args:
        *who: User IDs (``int``) and handles (``str``, with or without ``@``, in any case).

    Returns:
        A filter that matches when the sender (``from_user``) is one of them. A post that
        speaks as the channel has no sender, and never matches.

    Example:
        A command only the admins can use::

            @bot.command("stats", filters=(f.user(*ADMINS),))
            async def stats(message): ...
    """
    ids, names = _ids_and_names(who)

    def check(event: Any) -> bool:
        sender = getattr(event, "from_user", None)
        if not isinstance(sender, User):
            return False
        return sender.id in ids or (sender.username or "").lower() in names

    return Filter(check, "user")


def chat(*which: int | str) -> Filter:
    """Makes a filter for events in one of these chats.

    Args:
        *which: Chat IDs (``int``) and the public handles of groups and channels (``str``,
            with or without ``@``, in any case).

    Returns:
        A filter that matches when the event's chat is one of them.
    """
    ids, names = _ids_and_names(which)

    def check(event: Any) -> bool:
        c = _chat_of(event)
        if c is None:
            return False
        return c.id in ids or (c.username or "").lower() in names

    return Filter(check, "chat")


def callback_data(*values: str, prefix: str | None = None) -> Filter:
    """Makes a filter for button presses with this data.

    `pgpcbot.Router.on_callback_query` does the same with its ``data`` and ``prefix``
    arguments.

    Args:
        *values: Data to take as it is, such as ``"vote:yes"``.
        prefix: Data to take by its start, such as ``"vote:"``.

    Returns:
        A filter that matches a button press whose data is one of ``values``, or starts with
        ``prefix``. With neither, nothing matches.
    """
    wanted = set(values)

    def check(event: Any) -> bool:
        if not isinstance(event, CallbackQueryBase):
            return False
        if wanted and event.data in wanted:
            return True
        return prefix is not None and event.data.startswith(prefix)

    return Filter(check, "callback_data")
