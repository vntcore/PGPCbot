from __future__ import annotations

from typing import Any

import pytest

from pgpcbot import Button, InlineKeyboard, KeyboardBuilder, ValidationError
from pgpcbot.keyboard import markup_payload

EMOJI = "\U0001f600"


def cb(i: int) -> Button:
    return Button.callback(f"B{i}", f"d{i}")


def rows_of(count: int, per_row: int) -> list[list[Button]]:
    buttons = [cb(i) for i in range(count)]
    return [buttons[i : i + per_row] for i in range(0, count, per_row)]


def single(button: Any) -> dict[str, Any] | None:
    return markup_payload([[button]])


def rows_in(markup: Any) -> list[list[dict[str, str]]]:
    payload = markup_payload(markup)
    assert payload is not None
    rows: list[list[dict[str, str]]] = payload["inlineKeyboard"]
    return rows


def stringify(rows: list[list[Button]]) -> str:
    """JSON.stringify of the markup, written out by hand: for strings that need no escaping."""

    def button(b: Button) -> str:
        return f'{{"text":"{b.text}","callbackData":"{b.callback_data}"}}'

    return '{"inlineKeyboard":[' + ",".join("[" + ",".join(map(button, row)) + "]" for row in rows) + "]}"


def keyboard_of_json_size(size: int) -> list[list[Button]]:
    # 7 rows of 8 buttons, each with 64 bytes of data; the last label absorbs the difference
    rows = [[Button.callback("t" * 52, f"{r}{c}".ljust(64, "x")) for c in range(8)] for r in range(7)]
    current = len(stringify(rows).encode())
    last = rows[-1][-1]
    rows[-1][-1] = Button.callback("t" * (len(last.text) + size - current), last.callback_data)
    assert len(stringify(rows).encode()) == size
    return rows


def test_button_constructors_set_exactly_one_target() -> None:
    assert Button.callback("Hourly", "hourly:rome") == Button("Hourly", callback_data="hourly:rome")
    assert Button.link("Web", "https://example.org") == Button("Web", url="https://example.org")


def test_button_json_round_trip() -> None:
    for button in (Button.callback("Yes", "yes"), Button.link("Docs", "https://example.org/docs")):
        assert Button.from_json(button.to_json()) == button
    assert Button.callback("Yes", "yes").to_json() == {"text": "Yes", "callbackData": "yes"}
    assert Button.link("Docs", "https://e.org").to_json() == {"text": "Docs", "url": "https://e.org"}


def test_button_from_json_ignores_fields_that_are_not_strings() -> None:
    assert Button.from_json({"text": "x", "callbackData": 5, "url": None}) == Button("x")


def test_button_is_immutable() -> None:
    button = Button.callback("a", "b")
    with pytest.raises(AttributeError):
        button.text = "c"  # type: ignore[misc]


def test_no_markup_is_none() -> None:
    assert markup_payload(None) is None


def test_inline_keyboard_becomes_the_wire_json() -> None:
    kb = InlineKeyboard.build(
        [Button.callback("Hourly", "hourly:rome"), Button.callback("Another town", "ask")],
        [Button.link("Open on the web", "https://example.org/rome")],
    )
    assert markup_payload(kb) == {
        "inlineKeyboard": [
            [{"text": "Hourly", "callbackData": "hourly:rome"}, {"text": "Another town", "callbackData": "ask"}],
            [{"text": "Open on the web", "url": "https://example.org/rome"}],
        ]
    }


def test_nested_lists_and_tuples_are_accepted() -> None:
    expected = {"inlineKeyboard": [[{"text": "A", "callbackData": "a"}], [{"text": "B", "callbackData": "b"}]]}
    assert markup_payload([[Button.callback("A", "a")], [Button.callback("B", "b")]]) == expected
    assert markup_payload(((Button.callback("A", "a"),), (Button.callback("B", "b"),))) == expected


def test_dict_markup_is_checked_and_normalised() -> None:
    given = {"inlineKeyboard": [[{"text": "  Yes ", "callbackData": "y"}, {"text": "Site", "url": "https://e.org"}]]}
    assert markup_payload(given) == {
        "inlineKeyboard": [[{"text": "Yes", "callbackData": "y"}, {"text": "Site", "url": "https://e.org"}]]
    }


def test_dict_markup_with_other_top_level_keys_is_refused() -> None:
    with pytest.raises(ValidationError, match="nothing else") as info:
        markup_payload({"inlineKeyboard": [[cb(1)]], "resize": True})
    assert info.value.field == "markup"
    with pytest.raises(ValidationError, match="nothing else"):
        markup_payload({"inline_keyboard": [[cb(1)]]})


def test_dict_button_with_an_unknown_field_is_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="row 0, button 0: unknown field 'callback_data'"):
        markup_payload({"inlineKeyboard": [[{"text": "A", "callback_data": "a"}]]})


def test_dict_button_with_a_null_text_is_refused() -> None:
    with pytest.raises(ValidationError, match="text is required"):
        markup_payload({"inlineKeyboard": [[{"text": None, "callbackData": "a"}]]})


@pytest.mark.parametrize("markup", ["Yes|No", 42, {cb(1)}, cb(1)])
def test_markup_of_another_type_is_refused(markup: Any) -> None:
    with pytest.raises(ValidationError, match="markup must be an InlineKeyboard") as info:
        markup_payload(markup)
    assert info.value.field == "markup"


def test_inline_keyboard_value_must_be_a_list_of_rows() -> None:
    with pytest.raises(ValidationError, match="inlineKeyboard must be a list of rows"):
        markup_payload({"inlineKeyboard": "rows"})


def test_a_flat_list_of_buttons_is_refused_as_a_row() -> None:
    # the usual slip: [b1, b2] instead of [[b1, b2]]
    with pytest.raises(ValidationError, match="row 0: must be a list of buttons"):
        markup_payload([cb(1), cb(2)])


def test_something_that_is_not_a_button_in_a_row_is_refused() -> None:
    with pytest.raises(ValidationError, match="row 1, button 0: a button must be a Button, got str"):
        markup_payload([[cb(1)], ["Yes"]])


def test_empty_rows_are_dropped() -> None:
    payload = markup_payload([[], [cb(1)], [], [cb(2)], ()])
    assert payload == {
        "inlineKeyboard": [[{"text": "B1", "callbackData": "d1"}], [{"text": "B2", "callbackData": "d2"}]]
    }


@pytest.mark.parametrize(
    "empty",
    [
        [],
        [[]],
        [[], ()],
        InlineKeyboard(),
        InlineKeyboard.build([]),
        {"inlineKeyboard": []},
        {"inlineKeyboard": [[], []]},
    ],
)
def test_a_keyboard_with_no_buttons_is_no_keyboard(empty: Any) -> None:
    assert markup_payload(empty) is None


def test_twenty_rows_pass_and_twenty_one_do_not() -> None:
    assert len(rows_in(rows_of(20, 1))) == 20
    with pytest.raises(ValidationError, match="more than 20 rows"):
        markup_payload(rows_of(21, 1))


def test_eight_buttons_a_row_pass_and_nine_do_not() -> None:
    assert len(rows_in(rows_of(8, 8))[0]) == 8
    with pytest.raises(ValidationError, match="row 1: more than 8 buttons"):
        markup_payload([[cb(100)], [cb(i) for i in range(9)]])


def test_a_hundred_buttons_pass_and_a_hundred_and_one_do_not() -> None:
    assert sum(map(len, rows_in(rows_of(100, 8)))) == 100
    with pytest.raises(ValidationError, match="more than 100 buttons in all"):
        markup_payload(rows_of(101, 8))


@pytest.mark.parametrize("text", ["a" * 64, EMOJI * 32, "è" * 64])
def test_label_up_to_64_utf16_units_passes(text: str) -> None:
    assert single(Button.callback(text, "x")) == {"inlineKeyboard": [[{"text": text, "callbackData": "x"}]]}


@pytest.mark.parametrize("text", ["a" * 65, EMOJI * 33, EMOJI * 32 + "a"])
def test_label_over_64_utf16_units_is_refused(text: str) -> None:
    with pytest.raises(ValidationError, match="row 0, button 0: text is longer than 64"):
        single(Button.callback(text, "x"))


def test_label_is_trimmed_like_javascript() -> None:
    payload = single(Button.callback("\ufeff  Yes \u3000", "y"))
    assert payload == {"inlineKeyboard": [[{"text": "Yes", "callbackData": "y"}]]}


@pytest.mark.parametrize("text", ["", "   ", "\ufeff", None, 5])
def test_label_is_required(text: Any) -> None:
    with pytest.raises(ValidationError, match="text is required"):
        single(Button(text, callback_data="x"))


@pytest.mark.parametrize("char", ["\n", "\t", "\x01", "\x7f", "\x85"])
def test_label_with_a_control_character_is_refused(char: str) -> None:
    with pytest.raises(ValidationError, match="text contains control characters"):
        single(Button.callback(f"Ye{char}s", "y"))


def test_button_with_neither_target_is_refused() -> None:
    with pytest.raises(ValidationError, match="exactly one of callback_data or url"):
        single(Button("Nowhere"))


def test_button_with_both_targets_is_refused() -> None:
    with pytest.raises(ValidationError, match="exactly one of callback_data or url"):
        single(Button("Both", callback_data="x", url="https://example.org"))


@pytest.mark.parametrize("data", ["a" * 64, "è" * 32, EMOJI * 16])
def test_callback_data_up_to_64_utf8_bytes_passes(data: str) -> None:
    assert single(Button.callback("x", data)) == {"inlineKeyboard": [[{"text": "x", "callbackData": data}]]}


@pytest.mark.parametrize("data", ["a" * 65, "è" * 33, EMOJI * 16 + "a"])
def test_callback_data_over_64_utf8_bytes_is_refused(data: str) -> None:
    # "è" * 33 is 33 characters: the limit is on bytes, not characters
    with pytest.raises(ValidationError, match="callback_data is longer than 64 bytes"):
        single(Button.callback("x", data))


@pytest.mark.parametrize("data", ["", 7])
def test_callback_data_must_be_a_non_empty_string(data: Any) -> None:
    with pytest.raises(ValidationError, match="callback_data must be a non-empty string"):
        single(Button("x", callback_data=data))


def test_callback_data_with_a_control_character_is_refused() -> None:
    with pytest.raises(ValidationError, match="callback_data contains control characters"):
        single(Button.callback("x", "page\x002"))


def test_callback_data_is_sent_exactly_as_given() -> None:
    assert rows_in([[Button.callback("x", "  spaced  ")]]) == [[{"text": "x", "callbackData": "  spaced  "}]]


def test_callback_data_must_be_unique_across_the_whole_keyboard() -> None:
    with pytest.raises(ValidationError, match="callback_data 'dup' is used twice"):
        markup_payload([[Button.callback("A", "dup")], [Button.callback("B", "other"), Button.callback("C", "dup")]])


def test_link_buttons_do_not_take_part_in_the_uniqueness_check() -> None:
    url = "https://example.org"
    assert markup_payload([[Button.link("A", url), Button.link("B", url)]]) is not None


@pytest.mark.parametrize(
    "url",
    ["ftp://example.org", "https://user:pw@example.org", "https://user@example.org", "example.org", "https://:99999"],
)
def test_link_url_follows_the_url_rules(url: str) -> None:
    with pytest.raises(ValidationError, match="row 0, button 0: ") as info:
        single(Button.link("Go", url))
    assert info.value.field == "markup"


def test_link_url_up_to_1024_characters_passes() -> None:
    url = "https://example.org/" + "p" * (1024 - len("https://example.org/"))
    assert single(Button.link("Go", url)) == {"inlineKeyboard": [[{"text": "Go", "url": url}]]}
    with pytest.raises(ValidationError, match="longer than 1024"):
        single(Button.link("Go", url + "p"))


def test_link_url_must_be_a_string() -> None:
    with pytest.raises(ValidationError, match="non-empty string"):
        single(Button("Go", url=b"https://example.org"))  # type: ignore[arg-type]


def test_the_error_names_the_faulty_button() -> None:
    rows = [[cb(1), cb(2)], [cb(3), cb(4), Button.callback("x" * 65, "long")]]
    with pytest.raises(ValidationError, match=r"^row 1, button 2: text is longer"):
        markup_payload(rows)


def test_json_of_exactly_8_kib_passes() -> None:
    rows = keyboard_of_json_size(8192)
    assert markup_payload(rows) is not None


def test_json_over_8_kib_is_refused() -> None:
    rows = keyboard_of_json_size(8193)
    with pytest.raises(ValidationError, match="8193 bytes of JSON, the limit is 8192") as info:
        markup_payload(rows)
    assert info.value.field == "markup"


def test_json_size_counts_utf8_bytes_not_escapes() -> None:
    # 32 labels of 64 "è": 7132 bytes as UTF-8, about three times that as \u escapes
    rows = [[Button.callback("è" * 64, "é" * 31 + f"{r}{c}") for c in range(8)] for r in range(4)]
    assert len(stringify(rows).encode()) == 7132
    assert markup_payload(rows) is not None


def test_json_size_is_measured_on_the_trimmed_labels() -> None:
    rows = keyboard_of_json_size(8192)
    rows[0][0] = Button.callback("  " + rows[0][0].text, rows[0][0].callback_data)
    assert markup_payload(rows) is not None


def test_build_takes_one_argument_per_row() -> None:
    kb = InlineKeyboard.build([cb(1), cb(2)], (cb(3),))
    assert kb.rows == ((cb(1), cb(2)), (cb(3),))
    assert len(kb) == 2
    assert kb.buttons() == [cb(1), cb(2), cb(3)]


def test_find_returns_the_button_with_that_data() -> None:
    kb = InlineKeyboard.build([cb(1), Button.link("web", "https://e.org")], [cb(2)])
    assert kb.find("d2") == cb(2)
    assert kb.find("missing") is None


def test_keyboard_round_trips_through_the_wire_json() -> None:
    kb = InlineKeyboard.build([cb(1), Button.link("Web", "https://example.org")], [cb(2)])
    assert InlineKeyboard.from_json(kb.to_json()) == kb
    assert InlineKeyboard.from_json(markup_payload(kb)) == kb


def test_round_trip_through_the_payload_trims_labels() -> None:
    kb = InlineKeyboard.build([Button.callback(" Yes ", "y")])
    assert InlineKeyboard.from_json(markup_payload(kb)) == InlineKeyboard.build([Button.callback("Yes", "y")])


@pytest.mark.parametrize("data", [None, [], "x", {"rows": []}, {"inlineKeyboard": "x"}])
def test_from_json_without_a_keyboard_is_none(data: Any) -> None:
    assert InlineKeyboard.from_json(data) is None


def test_from_json_skips_rows_and_buttons_of_the_wrong_shape() -> None:
    kb = InlineKeyboard.from_json({"inlineKeyboard": [[{"text": "A", "callbackData": "a"}, "junk"], "junk", []]})
    assert kb == InlineKeyboard(((Button.callback("A", "a"),), ()))


def test_builder_wraps_rows_as_they_fill() -> None:
    kb = KeyboardBuilder(per_row=3)
    for i in range(7):
        kb.add(cb(i))
    assert kb.build().rows == ((cb(0), cb(1), cb(2)), (cb(3), cb(4), cb(5)), (cb(6),))


def test_builder_add_takes_several_buttons_at_once() -> None:
    kb = KeyboardBuilder(per_row=2).add(cb(0), cb(1), cb(2)).build()
    assert kb.rows == ((cb(0), cb(1)), (cb(2),))


def test_builder_row_stands_alone_and_the_next_add_starts_a_new_row() -> None:
    kb = KeyboardBuilder(per_row=3).add(cb(0)).row(cb(1), cb(2)).add(cb(3)).add(cb(4)).build()
    assert kb.rows == ((cb(0),), (cb(1), cb(2)), (cb(3), cb(4)))


def test_builder_row_is_not_limited_by_per_row() -> None:
    # per_row only shapes add(); markup_payload still holds every row to 8
    kb = KeyboardBuilder(per_row=1).row(cb(0), cb(1), cb(2)).build()
    assert kb.rows == ((cb(0), cb(1), cb(2)),)


def test_builder_drops_empty_rows() -> None:
    kb = KeyboardBuilder().row().add(cb(0)).row().build()
    assert kb.rows == ((cb(0),),)
    assert KeyboardBuilder().build() == InlineKeyboard()


def test_builder_defaults_to_eight_a_row() -> None:
    kb = KeyboardBuilder()
    kb.add(*(cb(i) for i in range(9)))
    assert [len(row) for row in kb.build().rows] == [8, 1]


@pytest.mark.parametrize("per_row", [0, 9, -1])
def test_builder_per_row_must_be_between_1_and_8(per_row: int) -> None:
    with pytest.raises(ValueError, match="per_row must be between 1 and 8"):
        KeyboardBuilder(per_row=per_row)


def test_builder_output_is_a_valid_payload() -> None:
    kb = KeyboardBuilder(per_row=4)
    kb.add(*(cb(i) for i in range(10)))
    kb.row(Button.link("Help", "https://example.org/help"))
    assert [len(row) for row in rows_in(kb.build())] == [4, 4, 2, 1]
