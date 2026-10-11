"""Inline keyboards: the rows of buttons under a bot's message.

A `~pgpcbot.Button` is a callback button, which sends a short piece of data back to the bot
when pressed, or a link button, which offers to open a web address. An
`~pgpcbot.InlineKeyboard` holds the rows of buttons, and a `~pgpcbot.KeyboardBuilder` fills the
rows for you. Pass the keyboard as ``markup=`` to the calls that send or edit a message::

    keyboard = InlineKeyboard.build(
        [Button.callback("Hourly", "hourly:rome"), Button.callback("Another town", "ask")],
        [Button.link("Open on the web", "https://example.org/rome")],
    )
    await message.reply("Rome: sun, 24 degrees.", markup=keyboard)

Nothing is checked while a keyboard is built. When it is sent, the library checks it against
the server's rules (listed in `~pgpcbot.InlineKeyboard` and `~pgpcbot.Button`) and raises
`~pgpcbot.ValidationError` if one is broken.

Warning:
    Buttons are never encrypted, in any kind of chat: their text and their data travel in
    clear, because the server has to know which button was pressed. The message above them is
    encrypted as usual.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import _validate as v
from .errors import ValidationError

__all__ = ["Button", "InlineKeyboard", "KeyboardBuilder"]

ROWS_MAX = 20
PER_ROW_MAX = 8
BUTTONS_MAX = 100
BUTTON_TEXT_MAX = 64
CALLBACK_DATA_BYTES = 64
MARKUP_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class Button:
    """One button under a message: a callback button or a link button.

    A callback button sends its ``callback_data`` back to the bot when pressed, as a
    `CallbackQuery`. A link button offers to open its ``url``, and the phone shows the address
    before opening it. Make one with `callback` or `link`; the fields are also the
    constructor's arguments, and exactly one of ``callback_data`` and ``url`` must be set.

    Nothing is checked when a button is made. When its keyboard is sent, a broken rule raises
    `ValidationError` (with ``field`` set to ``"markup"``):

    - ``text`` is not blank, has at most 64 characters (UTF-16 units) and no control
      characters. Spaces at both ends are trimmed when it is sent.
    - ``callback_data`` has 1 to 64 bytes in UTF-8 and no control characters, and no other
      button of the keyboard has the same.
    - ``url`` is an absolute ``http`` or ``https`` address with no user name or password, at
      most 1024 characters long.

    Attributes:
        text: The label on the button.
        callback_data: The data a press sends back to the bot. ``None`` for a link button.
        url: The address a link button opens. ``None`` for a callback button.

    Example:
        >>> from pgpcbot import Button
        >>> Button.callback("Yes", "vote:yes")
        Button(text='Yes', callback_data='vote:yes', url=None)
        >>> Button.link("Rules", "https://example.org/rules").to_json()
        {'text': 'Rules', 'url': 'https://example.org/rules'}

    Warning:
        Buttons are never encrypted, not even in an encrypted chat: the server has to know
        which button was pressed. Keep personal data out of ``text`` and ``callback_data``.
        Use an ID that means something only to your bot (``order:8812``), not the content
        (``order:Anna Rossi, 3 pizzas``).
    """

    text: str
    callback_data: str | None = None
    url: str | None = None

    @classmethod
    def callback(cls, text: str, data: str) -> Button:
        """Makes a callback button, which sends ``data`` back to the bot when pressed.

        The press reaches the handlers of `Router.on_callback_query` as a `CallbackQuery`
        whose ``data`` is this string.

        Args:
            text: The label on the button.
            data: 1 to 64 bytes in UTF-8 that tell the bot which button it was, such as
                ``"vote:yes"``. Never encrypted.

        Returns:
            The new button.
        """
        return cls(text, callback_data=data)

    @classmethod
    def link(cls, text: str, url: str) -> Button:
        """Makes a link button, which offers to open ``url``.

        The phone shows the address before opening it.

        Args:
            text: The label on the button.
            url: An absolute ``http`` or ``https`` address, up to 1024 characters, with no
                user name or password in it.

        Returns:
            The new button.
        """
        return cls(text, url=url)

    def to_json(self) -> dict[str, str]:
        """Converts the button to the API's JSON form.

        Nothing is checked or trimmed here.

        Returns:
            A dict, ``{"text": ..., "callbackData": ...}`` or ``{"text": ..., "url": ...}``.
        """
        out = {"text": self.text}
        if self.callback_data is not None:
            out["callbackData"] = self.callback_data
        if self.url is not None:
            out["url"] = self.url
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Button:
        """Makes a button from the API's JSON form, the reverse of `to_json`.

        Nothing is checked. A ``callbackData`` or ``url`` that is not a string is left out.

        Args:
            data: One button as the API writes it, such as
                ``{"text": "Yes", "callbackData": "vote:yes"}``.

        Returns:
            The button.
        """
        return cls(
            str(data.get("text", "")),
            callback_data=data.get("callbackData") if isinstance(data.get("callbackData"), str) else None,
            url=data.get("url") if isinstance(data.get("url"), str) else None,
        )


def _check_button(button: Any, where: str) -> dict[str, str]:
    if isinstance(button, Mapping):
        extra = set(button) - {"text", "callbackData", "url"}
        if extra:
            raise ValidationError(f"{where}: unknown field {min(extra)!r}", field="markup")
        # types first: from_json would turn a null label into "None" and drop a numeric callbackData
        if not isinstance(button.get("text"), str):
            raise ValidationError(f"{where}: text is required", field="markup")
        for key in ("callbackData", "url"):
            if button.get(key) is not None and not isinstance(button.get(key), str):
                raise ValidationError(f"{where}: {key} must be a string", field="markup")
        button = Button.from_json(button)
    if not isinstance(button, Button):
        raise ValidationError(f"{where}: a button must be a Button, got {type(button).__name__}", field="markup")

    text: object = button.text
    if not isinstance(text, str) or not v.js_trim(text):
        raise ValidationError(f"{where}: text is required", field="markup")
    if v.utf16_len(text) > BUTTON_TEXT_MAX:
        raise ValidationError(f"{where}: text is longer than {BUTTON_TEXT_MAX} characters", field="markup")
    if v.has_control(text):
        raise ValidationError(f"{where}: text contains control characters", field="markup")
    if (button.callback_data is None) == (button.url is None):
        raise ValidationError(f"{where}: give exactly one of callback_data or url", field="markup")

    if button.callback_data is not None:
        data: object = button.callback_data
        if not isinstance(data, str) or data == "":
            raise ValidationError(f"{where}: callback_data must be a non-empty string", field="markup")
        if len(data.encode("utf-8")) > CALLBACK_DATA_BYTES:
            raise ValidationError(
                f"{where}: callback_data is longer than {CALLBACK_DATA_BYTES} bytes (UTF-8)", field="markup"
            )
        if v.has_control(data):
            raise ValidationError(f"{where}: callback_data contains control characters", field="markup")
        return {"text": v.js_trim(text), "callbackData": data}

    try:
        url = v.check_http_url(button.url, field="url")
    except ValidationError as exc:
        raise ValidationError(f"{where}: {exc}", field="markup") from None
    return {"text": v.js_trim(text), "url": url}


@dataclass(frozen=True, slots=True)
class InlineKeyboard:
    """The rows of buttons under a message.

    Make one with `build`, one argument per row, or with a `KeyboardBuilder`. The constructor
    takes the rows as a tuple of tuples. Pass the keyboard as ``markup=`` when sending or
    editing a message (`Message.reply`, `Bot.send_message`, `CallbackQuery.edit_message`...).
    Wherever a keyboard is accepted, the API's JSON form works too, as a ``dict``.
    ``len(keyboard)`` is the number of rows.

    Nothing is checked while the keyboard is built. When it is sent, a broken rule raises
    `ValidationError` (with ``field`` set to ``"markup"``) before anything leaves the process:

    - up to 20 rows, 8 buttons in a row and 100 buttons in all;
    - each button follows the rules listed in `Button`, and no two buttons have the same
      ``callback_data``;
    - the whole keyboard is at most 8 KiB (8192 bytes) of JSON, measured the way the server
      measures it.

    Empty rows are dropped, and a keyboard with no buttons is the same as no keyboard. Its
    buttons are never encrypted (see `Button`).

    Attributes:
        rows: The rows, top to bottom, each a tuple of buttons from left to right.

    Example:
        >>> from pgpcbot import Button, InlineKeyboard
        >>> keyboard = InlineKeyboard.build(
        ...     [Button.callback("Yes", "vote:yes"), Button.callback("No", "vote:no")],
        ...     [Button.link("Rules", "https://example.org/rules")],
        ... )
        >>> len(keyboard)
        2
        >>> keyboard.find("vote:no").text
        'No'

        In a handler, the keyboard goes under the reply::

            await message.reply("Do you agree?", markup=keyboard)
    """

    rows: tuple[tuple[Button, ...], ...] = field(default_factory=tuple)

    @classmethod
    def build(cls, *rows: Iterable[Button]) -> InlineKeyboard:
        """Makes a keyboard from rows of buttons, one argument per row.

        ``InlineKeyboard.build([yes, no], [rules])`` puts ``yes`` and ``no`` side by side,
        and ``rules`` under them.

        Args:
            *rows: The rows, top to bottom. Each is a list (or any iterable) of buttons,
                from left to right.

        Returns:
            The keyboard.
        """
        return cls(tuple(tuple(row) for row in rows))

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> InlineKeyboard | None:
        """Makes a keyboard from the API's JSON form, the reverse of `to_json`.

        Nothing is checked. Rows that are not lists and buttons that are not objects are
        skipped.

        Args:
            data: ``{"inlineKeyboard": [[...], ...]}``, as the API writes it, or ``None``.

        Returns:
            The keyboard, or ``None`` when ``data`` is not a mapping with an
            ``inlineKeyboard`` list.
        """
        if not isinstance(data, Mapping):
            return None
        rows = data.get("inlineKeyboard")
        if not isinstance(rows, list):
            return None
        return cls(
            tuple(
                tuple(Button.from_json(b) for b in row if isinstance(b, Mapping))
                for row in rows
                if isinstance(row, list)
            )
        )

    def to_json(self) -> dict[str, Any]:
        """Converts the keyboard to the API's JSON form.

        Nothing is checked or dropped here: the rules apply when the keyboard is sent.

        Returns:
            A dict, ``{"inlineKeyboard": [[...], ...]}``, with each button as `Button.to_json`
            writes it.
        """
        return {"inlineKeyboard": [[b.to_json() for b in row] for row in self.rows]}

    def buttons(self) -> list[Button]:
        """Lists every button of the keyboard.

        Returns:
            The buttons, row by row from the top, each row from left to right.
        """
        return [b for row in self.rows for b in row]

    def find(self, callback_data: str) -> Button | None:
        """Finds the button that carries ``callback_data``.

        Args:
            callback_data: The data to look for, such as the ``data`` of a `CallbackQuery`.

        Returns:
            The first button with that data, or ``None`` when no button has it.
        """
        for b in self.buttons():
            if b.callback_data == callback_data:
                return b
        return None

    def __len__(self) -> int:
        return len(self.rows)


class KeyboardBuilder:
    """Builds a keyboard a row at a time, starting a new row when one is full.

    `add` puts buttons in the current row and starts a new row after ``per_row`` buttons;
    `row` adds a row of its own. Both give the builder back, so calls can be chained, and
    `build` makes the `InlineKeyboard`. Nothing is checked until the keyboard is sent.

    Example:
        >>> from pgpcbot import Button, KeyboardBuilder
        >>> towns = ["Rome", "Milan", "Turin", "Bari"]
        >>> keyboard = (
        ...     KeyboardBuilder(per_row=3)
        ...     .add(*[Button.callback(town, f"town:{town.lower()}") for town in towns])
        ...     .row(Button.link("All towns", "https://example.org/towns"))
        ...     .build()
        ... )
        >>> [[button.text for button in row] for row in keyboard.rows]
        [['Rome', 'Milan', 'Turin'], ['Bari'], ['All towns']]
    """

    def __init__(self, *, per_row: int = PER_ROW_MAX) -> None:
        """Starts an empty keyboard.

        Args:
            per_row: How many buttons `add` puts in a row before it starts the next one, from
                1 to 8 (the server's limit). The default is 8.

        Raises:
            ValueError: When ``per_row`` is not between 1 and 8.
        """
        if not 1 <= per_row <= PER_ROW_MAX:
            raise ValueError(f"per_row must be between 1 and {PER_ROW_MAX}")
        self._per_row = per_row
        self._rows: list[list[Button]] = []
        self._open = False

    def add(self, *buttons: Button) -> KeyboardBuilder:
        """Adds buttons to the current row, starting a new row when it is full.

        A row is full when it has ``per_row`` buttons. After `row`, the next button starts a
        new row.

        Args:
            *buttons: The buttons, from left to right.

        Returns:
            This builder, so calls can be chained.
        """
        for button in buttons:
            if not self._open or len(self._rows[-1]) >= self._per_row:
                self._rows.append([])
                self._open = True
            self._rows[-1].append(button)
        return self

    def row(self, *buttons: Button) -> KeyboardBuilder:
        """Adds a row of its own with these buttons; the next `add` starts a new row.

        ``per_row`` does not apply to this row, but the server's limit of 8 buttons a row
        does. With no buttons, it only ends the current row.

        Args:
            *buttons: The buttons of the row, from left to right.

        Returns:
            This builder, so calls can be chained.
        """
        self._rows.append(list(buttons))
        self._open = False
        return self

    def build(self) -> InlineKeyboard:
        """Makes the keyboard from the rows added so far. Empty rows are left out.

        Returns:
            The `InlineKeyboard`.
        """
        return InlineKeyboard(tuple(tuple(r) for r in self._rows if r))


def markup_payload(
    markup: InlineKeyboard | Mapping[str, Any] | Sequence[Sequence[Button]] | None,
) -> dict[str, Any] | None:
    """Checks a keyboard the way the server does and returns its JSON, or None.

    Empty rows are dropped like the server drops them; a keyboard left with no
    rows at all is the same as no keyboard.
    """
    given: object = markup
    if given is None:
        return None
    if isinstance(given, InlineKeyboard):
        rows: Any = given.rows
    elif isinstance(given, Mapping):
        if set(given) != {"inlineKeyboard"}:
            raise ValidationError("markup must be {'inlineKeyboard': [...]} and nothing else", field="markup")
        rows = given["inlineKeyboard"]
    elif isinstance(given, (list, tuple)):
        rows = given
    else:
        raise ValidationError(f"markup must be an InlineKeyboard, got {type(given).__name__}", field="markup")

    if not isinstance(rows, (list, tuple)):
        raise ValidationError("inlineKeyboard must be a list of rows", field="markup")
    if len(rows) > ROWS_MAX:
        raise ValidationError(f"the keyboard has more than {ROWS_MAX} rows", field="markup")

    out: list[list[dict[str, str]]] = []
    seen: set[str] = set()
    total = 0
    for r, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise ValidationError(f"row {r}: must be a list of buttons", field="markup")
        if not row:
            continue
        if len(row) > PER_ROW_MAX:
            raise ValidationError(f"row {r}: more than {PER_ROW_MAX} buttons", field="markup")
        checked = [_check_button(b, f"row {r}, button {c}") for c, b in enumerate(row)]
        total += len(checked)
        if total > BUTTONS_MAX:
            raise ValidationError(f"more than {BUTTONS_MAX} buttons in all", field="markup")
        for b in checked:
            data = b.get("callbackData")
            if data is not None:
                if data in seen:
                    raise ValidationError(f"callback_data {data!r} is used twice", field="markup")
                seen.add(data)
        out.append(checked)

    if not out:
        return None
    payload = {"inlineKeyboard": out}
    # JSON.stringify output: no spaces, non-ascii kept as is
    size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > MARKUP_BYTES:
        raise ValidationError(f"the keyboard is {size} bytes of JSON, the limit is {MARKUP_BYTES}", field="markup")
    return payload
