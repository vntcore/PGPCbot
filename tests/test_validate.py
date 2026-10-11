from __future__ import annotations

import re
import uuid

import pytest

from pgpcbot import _validate as v
from pgpcbot.errors import PGPCbotError, ValidationError

from .conftest import fixture_text

EMOJI = "\U0001f600"  # outside the BMP: one character to Python, two to JavaScript


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("", 0),
        ("abc", 3),
        ("è già", 5),
        (EMOJI, 2),
        ("a" + EMOJI + "b", 4),
        ("\U0001f1ee\U0001f1f9", 4),  # a flag is two regional indicators
        ("\U0001f469\u200d\U0001f4bb", 5),  # ZWJ sequence: 2 + 1 + 2
    ],
)
def test_utf16_len_counts_like_javascript_length(text: str, units: int) -> None:
    assert v.utf16_len(text) == units


def test_utf16_len_counts_a_lone_surrogate_as_one_unit() -> None:
    # str can hold one (e.g. a filename decoded with surrogateescape); JS "\ud800".length === 1
    assert v.utf16_len("a\ud800b") == 3


def test_js_trim_drops_what_string_trim_drops() -> None:
    spaces = " \t\n\x0b\x0c\r\xa0\u1680\u2000\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
    assert v.js_trim(spaces + "ciao" + spaces) == "ciao"


def test_js_trim_drops_the_byte_order_mark_that_str_strip_keeps() -> None:
    assert "\ufeffhi".strip() == "\ufeffhi"
    assert v.js_trim("\ufeffhi\ufeff") == "hi"


@pytest.mark.parametrize("edge", ["\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\u200b"])
def test_js_trim_keeps_what_javascript_does_not_count_as_space(edge: str) -> None:
    assert v.js_trim(edge + "x" + edge) == edge + "x" + edge


@pytest.mark.parametrize(
    ("char", "control"),
    [
        ("\x00", True),
        ("\x1f", True),
        ("\x7f", True),
        ("\x9f", True),
        ("\t", True),
        ("\n", True),
        (" ", False),
        ("\xa0", False),
        ("\u2028", False),
        ("é", False),
    ],
)
def test_has_control_covers_c0_del_and_c1(char: str, control: bool) -> None:
    assert v.has_control(f"a{char}b") is control


def test_normalize_newlines_turns_crlf_and_lone_cr_into_lf() -> None:
    assert v.normalize_newlines("a\r\nb\rc\nd\r\n\r") == "a\nb\nc\nd\n\n"


def test_check_id_returns_a_positive_int() -> None:
    assert v.check_id(42, "chat_id") == 42


@pytest.mark.parametrize("value", [True, False, 0, -7, 1.0, "5", None])
def test_check_id_refuses_anything_but_a_positive_int(value: object) -> None:
    with pytest.raises(ValidationError, match="chat_id must be a positive integer") as info:
        v.check_id(value, "chat_id")
    assert info.value.field == "chat_id"


def test_validation_error_is_a_pgpcbot_error_and_a_value_error() -> None:
    with pytest.raises(ValidationError) as info:
        v.check_id(True, "chat_id")
    assert isinstance(info.value, PGPCbotError)
    assert isinstance(info.value, ValueError)


def test_check_text_accepts_exactly_4096_units() -> None:
    assert v.check_text("a" * 4096) == "a" * 4096
    assert v.check_text(EMOJI * 2048) == EMOJI * 2048


@pytest.mark.parametrize("text", ["a" * 4097, EMOJI * 2048 + "a", EMOJI * 2049])
def test_check_text_refuses_more_than_4096_utf16_units(text: str) -> None:
    with pytest.raises(ValidationError, match="the limit is 4096") as info:
        v.check_text(text)
    assert info.value.field == "text"


def test_check_text_counts_emoji_as_two_even_when_python_sees_fewer_characters() -> None:
    # 2049 python characters, 4098 for the server
    text = EMOJI * 2049
    assert len(text) < 4096
    with pytest.raises(ValidationError, match="4098 characters long"):
        v.check_text(text)


def test_check_text_measures_after_folding_crlf() -> None:
    # 4097 characters as given, 4096 once \r\n is one \n
    text = "a" * 4095 + "\r\n"
    assert v.check_text(text) == "a" * 4095 + "\n"


def test_check_text_normalises_carriage_returns() -> None:
    assert v.check_text("one\r\ntwo\rthree") == "one\ntwo\nthree"


def test_check_text_keeps_tabs_newlines_and_surrounding_spaces() -> None:
    assert v.check_text("  col1\tcol2\nrow2  ") == "  col1\tcol2\nrow2  "


@pytest.mark.parametrize(
    ("char", "name"),
    [
        ("\x01", "U+0001"),
        ("\x0b", "U+000B"),
        ("\x0c", "U+000C"),
        ("\x1b", "U+001B"),
        ("\x7f", "U+007F"),
        ("\x85", "U+0085"),
        ("\x00", "U+0000"),
    ],
)
def test_check_text_refuses_control_characters_by_code_point(char: str, name: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(f"control character {name}")):
        v.check_text(f"hello{char}world")


@pytest.mark.parametrize("text", ["", "   ", "\n\t\r\n", "\ufeff", "\u3000\u2028"])
def test_check_text_refuses_text_that_trims_to_nothing(text: str) -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        v.check_text(text)


@pytest.mark.parametrize("value", [None, 42, b"bytes", ["a"]])
def test_check_text_refuses_non_strings(value: object) -> None:
    with pytest.raises(ValidationError, match="must be a string"):
        v.check_text(value)


def test_check_text_uses_the_given_field_and_limit() -> None:
    assert v.check_text("abcd", field="caption", limit=4) == "abcd"
    with pytest.raises(ValidationError, match="caption is 5 characters long") as info:
        v.check_text("abcde", field="caption", limit=4)
    assert info.value.field == "caption"


def test_check_armored_returns_the_armor_untouched() -> None:
    armored = fixture_text("msg_text_signed.asc")
    assert v.check_armored(armored) == armored


@pytest.mark.parametrize(
    "value", [None, "hello", "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...", b"-----BEGIN PGP MESSAGE-----"]
)
def test_check_armored_wants_a_pgp_message(value: object) -> None:
    with pytest.raises(ValidationError, match="ASCII-armored PGP MESSAGE") as info:
        v.check_armored(value)
    assert info.value.field == "armored"


def test_check_armored_allows_128_kib_by_default() -> None:
    armored = v.ARMOR_MESSAGE + "x" * (128 * 1024 - len(v.ARMOR_MESSAGE))
    assert v.check_armored(armored) == armored
    with pytest.raises(ValidationError, match="is 131073 bytes, the limit is 131072"):
        v.check_armored(armored + "x")


def test_check_armored_takes_a_custom_limit() -> None:
    armored = v.ARMOR_MESSAGE + "x" * 100
    assert v.check_armored(armored, limit=len(armored)) == armored
    with pytest.raises(ValidationError, match="is 127 bytes, the limit is 126"):
        v.check_armored(armored, limit=len(armored) - 1)


def test_check_send_id_takes_a_uuid_object() -> None:
    value = uuid.uuid4()
    assert v.check_send_id(value) == str(value)


@pytest.mark.parametrize(
    "given",
    [
        "6F9619FF-8B86-D011-B42D-00CF4FC964FF",
        "6f9619ff8b86d011b42d00cf4fc964ff",
        "{6f9619ff-8b86-d011-b42d-00cf4fc964ff}",
        "urn:uuid:6f9619ff-8b86-d011-b42d-00cf4fc964ff",
    ],
)
def test_check_send_id_normalises_every_uuid_spelling(given: str) -> None:
    assert v.check_send_id(given) == "6f9619ff-8b86-d011-b42d-00cf4fc964ff"


@pytest.mark.parametrize("value", ["", "not-a-uuid", "6f9619ff-8b86-d011-b42d-00cf4fc964f", 12345, None, True])
def test_check_send_id_refuses_what_is_not_a_uuid(value: object) -> None:
    with pytest.raises(ValidationError, match="send_id must be a UUID") as info:
        v.check_send_id(value)
    assert info.value.field == "send_id"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org",
        "http://example.org/path?q=1#frag",
        "HTTPS://EXAMPLE.ORG/",
        "https://example.org:8443/x",
        "https://[2001:db8::1]/",
        # new URL() forgives these for http(s): the server takes them, so the client does too
        "https:example.org",
        "http:///path-only",
        r"https:\\example.org\path",
        "  https://example.org/padded  ",
    ],
)
def test_check_http_url_accepts_absolute_http_urls_and_returns_them_as_given(url: str) -> None:
    assert v.check_http_url(url, field="url") == url


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.org/file", "absolute http or https"),
        ("javascript:alert(1)", "absolute http or https"),
        ("//example.org/path", "absolute http or https"),
        ("/relative/path", "absolute http or https"),
        ("https://user@example.org", "credentials"),
        ("https://user:secret@example.org", "credentials"),
        ("https://:secret@example.org", "credentials"),
        ("https://example.org:99999/", "not a valid URL"),
        ("https://example.org:port/", "not a valid URL"),
        ("https://[::1/", "not a valid URL"),
        ("", "non-empty string"),
    ],
)
def test_check_http_url_refusals(url: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason) as info:
        v.check_http_url(url, field="link")
    assert info.value.field == "link"


def test_check_http_url_refuses_non_strings() -> None:
    with pytest.raises(ValidationError, match="non-empty string"):
        v.check_http_url(None, field="url")


def test_check_http_url_length_is_counted_in_utf16_units() -> None:
    head = "https://example.org/"
    exactly = head + EMOJI * ((1024 - len(head)) // 2)
    assert v.utf16_len(exactly) == 1024
    assert v.check_http_url(exactly, field="url") == exactly
    with pytest.raises(ValidationError, match="longer than 1024"):
        v.check_http_url(exactly + "a", field="url")


def test_check_webhook_url_refuses_a_fragment_but_not_plain_http() -> None:
    assert v.check_webhook_url("http://bot.example.org/hook") == "http://bot.example.org/hook"
    assert v.check_webhook_url("https://bot.example.org/hook#") == "https://bot.example.org/hook#"
    with pytest.raises(ValidationError, match="fragment") as info:
        v.check_webhook_url("https://bot.example.org/hook#section")
    assert info.value.field == "url"


def test_check_webhook_url_applies_the_http_url_rules() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        v.check_webhook_url("https://me:pw@bot.example.org/hook")


@pytest.mark.parametrize("secret", ["a", "A-z_0-9", "x" * 256])
def test_check_secret_accepts_the_server_alphabet(secret: str) -> None:
    assert v.check_secret(secret) == secret


@pytest.mark.parametrize("secret", ["", "x" * 257, "with space", "plus+sign", "ümlaut", None, 1234])
def test_check_secret_refuses_anything_else(secret: object) -> None:
    with pytest.raises(ValidationError, match="1-256 characters") as info:
        v.check_secret(secret)
    assert info.value.field == "secret"


def test_check_secret_refuses_a_trailing_newline() -> None:
    # a secret read with readline() keeps its \n; the server would answer 400
    with pytest.raises(ValidationError):
        v.check_secret("s3cr3t\n")


@pytest.mark.parametrize("scope", [None, ""])
def test_check_scope_defaults_to_default(scope: object) -> None:
    assert v.check_scope(scope) == "default"


@pytest.mark.parametrize("scope", ["default", "private", "groups", "groupAdmins", "chat:1", "chat:123456789012"])
def test_check_scope_accepts_the_documented_scopes(scope: str) -> None:
    assert v.check_scope(scope) == scope


@pytest.mark.parametrize(
    "scope", ["Default", "groupadmins", "admins", "chat:", "chat:-5", "chat:1234567890123", "chat: 12", 5, True]
)
def test_check_scope_refuses_anything_else(scope: object) -> None:
    with pytest.raises(ValidationError, match="unknown scope") as info:
        v.check_scope(scope)
    assert info.value.field == "scope"


@pytest.mark.parametrize("scope", ["default\n", "chat:42\n"])
def test_check_scope_refuses_a_trailing_newline(scope: str) -> None:
    with pytest.raises(ValidationError):
        v.check_scope(scope)


@pytest.mark.parametrize("scope", ["chat:\u0661\u0662\u0663", "chat:\uff14\uff12"])
def test_check_scope_chat_ids_are_ascii_digits(scope: str) -> None:
    with pytest.raises(ValidationError):
        v.check_scope(scope)


@pytest.mark.parametrize(
    ("given", "clean"),
    [
        ("start", "start"),
        ("/Start", "start"),
        ("  /HELP  ", "help"),
        ("\ufeff/weather_2", "weather_2"),
        ("x" * 32, "x" * 32),
    ],
)
def test_clean_command_strips_the_slash_lowercases_and_trims(given: str, clean: str) -> None:
    assert v.clean_command(given) == clean


@pytest.mark.parametrize("given", ["", "/", "x" * 33, "with-dash", "two words", "città", "/sta rt", None, 42])
def test_clean_command_refuses_invalid_names(given: object) -> None:
    with pytest.raises(ValidationError, match="invalid command") as info:
        v.clean_command(given)
    assert info.value.field == "commands"


def test_clean_command_description_folds_whitespace_like_the_server() -> None:
    assert v.clean_command_description("  Shows   the\tweather\n\n in\u00a0Rome ", "w") == "Shows the weather in Rome"


def test_clean_command_description_measures_after_folding() -> None:
    # 302 characters given, 3 after the fold
    assert v.clean_command_description("a" + " " * 300 + "b", "x") == "a b"


def test_clean_command_description_limit_is_256_utf16_units() -> None:
    assert v.clean_command_description("d" * 256, "x") == "d" * 256
    assert v.clean_command_description(EMOJI * 128, "x") == EMOJI * 128
    for too_long in ("d" * 257, EMOJI * 128 + "d"):
        with pytest.raises(ValidationError, match="description of /x must be 1-256 characters"):
            v.clean_command_description(too_long, "x")


@pytest.mark.parametrize("description", ["", "   \n\t ", None, 7])
def test_clean_command_description_refuses_empty_or_non_strings(description: object) -> None:
    with pytest.raises(ValidationError, match="description of /help") as info:
        v.clean_command_description(description, "help")
    assert info.value.field == "commands"


@pytest.mark.parametrize("char", ["\x01", "\x7f", "\x85"])
def test_clean_command_description_refuses_control_characters(char: str) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        v.clean_command_description(f"bad{char}one", "help")


@pytest.mark.parametrize(
    ("given", "wanted"),
    [
        ("anna", "anna"),
        ("@anna", "anna"),
        ("  @Anna_99  ", "Anna_99"),
        ("@@anna", "anna"),
        ("abc", "abc"),
        ("a" * 32, "a" * 32),
        ("123456", "123456"),
        # whatever came off a file or a pasted line is trimmed before it goes
        # anywhere near the path, so no newline can ever reach the request
        ("anna\n", "anna"),
        ("\tanna ", "anna"),
    ],
)
def test_check_username_takes_the_at_off_and_leaves_the_case(given: str, wanted: str) -> None:
    assert v.check_username(given) == wanted


@pytest.mark.parametrize(
    "value",
    ["ab", "a" * 33, "@", "", "con-trattino", "con spazio", "con.punto", "pippo!", "a/b", None, 55, True],
)
def test_check_username_refuses_anything_that_is_not_a_handle(value: object) -> None:
    with pytest.raises(ValidationError, match="3-32 characters") as info:
        v.check_username(value)
    assert info.value.field == "username"


def test_check_fingerprint_accepts_v4_and_v6_and_uppercases() -> None:
    v4 = "a" * 39 + "f"
    v6 = "0123456789abcdef" * 4
    assert v.check_fingerprint(v4) == v4.upper()
    assert v.check_fingerprint(f"  {v6}\n") == v6.upper()


@pytest.mark.parametrize(
    "value",
    ["a" * 39, "a" * 41, "a" * 63, "a" * 65, "g" * 40, "ABCD " * 8 + "ABCD", "", None, 0x1234],
)
def test_check_fingerprint_refuses_other_lengths_and_non_hex(value: object) -> None:
    with pytest.raises(ValidationError, match="40 \\(v4\\) or 64 \\(v6\\) hex") as info:
        v.check_fingerprint(value)
    assert info.value.field == "fingerprint"


def test_check_public_key_accepts_a_real_public_key() -> None:
    public = fixture_text("bot.pub.asc")
    assert v.check_public_key(public) == public


def test_check_public_key_refuses_a_private_key_on_its_own() -> None:
    with pytest.raises(ValidationError, match="PGP PUBLIC KEY BLOCK") as info:
        v.check_public_key(fixture_text("bot.sec.asc"))
    assert info.value.field == "public_key"


def test_check_public_key_refuses_a_private_block_next_to_the_public_one() -> None:
    both = fixture_text("bot.pub.asc") + "\n" + fixture_text("bot.sec.asc")
    with pytest.raises(ValidationError, match="looks like a private key"):
        v.check_public_key(both)


@pytest.mark.parametrize(
    "value",
    [None, "", "-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQINBF...", "mQINBF...\n-----END PGP PUBLIC KEY BLOCK-----"],
)
def test_check_public_key_wants_both_armor_lines(value: object) -> None:
    with pytest.raises(ValidationError, match="ASCII-armored PGP PUBLIC KEY BLOCK"):
        v.check_public_key(value)


def test_check_public_key_refuses_more_than_64_kib() -> None:
    padded = v.ARMOR_PUBLIC_BEGIN + "\n" + "A" * (64 * 1024) + "\n" + v.ARMOR_PUBLIC_END
    with pytest.raises(ValidationError, match="longer than 65536"):
        v.check_public_key(padded)


def test_check_poll_leaves_out_what_was_not_given() -> None:
    assert v.check_poll(None, None, None) == {}
    assert v.check_poll(0, None, 0) == {"offset": 0, "timeout": 0}


def test_check_poll_accepts_the_bounds() -> None:
    assert v.check_poll(10, 1, 50) == {"offset": 10, "limit": 1, "timeout": 50}
    assert v.check_poll(0, 100, 0) == {"offset": 0, "limit": 100, "timeout": 0}


@pytest.mark.parametrize(
    ("offset", "limit", "timeout", "field"),
    [
        (-1, None, None, "offset"),
        (True, None, None, "offset"),
        (1.5, None, None, "offset"),
        (None, 0, None, "limit"),
        (None, 101, None, "limit"),
        (None, True, None, "limit"),
        (None, "10", None, "limit"),
        (None, None, -1, "timeout"),
        (None, None, 51, "timeout"),
        (None, None, False, "timeout"),
        (None, None, 2.5, "timeout"),
    ],
)
def test_check_poll_refuses_out_of_range_values(offset: object, limit: object, timeout: object, field: str) -> None:
    with pytest.raises(ValidationError) as info:
        v.check_poll(offset, limit, timeout)
    assert info.value.field == field


def test_check_callback_answer_with_nothing_is_nothing() -> None:
    assert v.check_callback_answer(None, None) == (None, None)


def test_check_callback_answer_trims_the_text_and_drops_a_blank_one() -> None:
    assert v.check_callback_answer("  Saved!  ", None) == ("Saved!", None)
    assert v.check_callback_answer(" \ufeff ", None) == (None, None)


def test_check_callback_answer_text_limit_is_200_utf16_units() -> None:
    assert v.check_callback_answer("a" * 200, None) == ("a" * 200, None)
    assert v.check_callback_answer(EMOJI * 100, None) == (EMOJI * 100, None)
    for too_long in ("a" * 201, EMOJI * 100 + "a"):
        with pytest.raises(ValidationError, match="longer than 200") as info:
            v.check_callback_answer(too_long, None)
        assert info.value.field == "text"


@pytest.mark.parametrize("text", ["two\nlines", "tab\tinside", "bell\x07"])
def test_check_callback_answer_text_must_be_one_line_without_controls(text: str) -> None:
    with pytest.raises(ValidationError, match="single line"):
        v.check_callback_answer(text, None)


def test_check_callback_answer_refuses_a_non_string_text() -> None:
    with pytest.raises(ValidationError, match="text must be a string"):
        v.check_callback_answer(42, None)


def test_check_callback_answer_checks_the_url() -> None:
    assert v.check_callback_answer(None, "https://example.org/game") == (None, "https://example.org/game")
    with pytest.raises(ValidationError, match="absolute http or https") as info:
        v.check_callback_answer("ok", "tg://resolve")
    assert info.value.field == "url"
