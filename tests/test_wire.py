from __future__ import annotations

import json
from typing import Any

import pytest

from pgpcbot import Media
from pgpcbot.crypto import BotKey
from pgpcbot.wire import ALBUM, CALL, FORWARD, LINK, MEDIA, PAY, STICKER, Body, parse_body

from .conftest import fixture_text
from .fakeapi import MEDIA_ID


def media_env(**fields: Any) -> dict[str, Any]:
    env: dict[str, Any] = {"media": MEDIA_ID, "kind": "image", "mime": "image/jpeg", "w": 640, "h": 480}
    env.update(fields)
    return env


def wrap(marker: str, payload: Any) -> str:
    return marker + json.dumps(payload)


def media_of(**fields: Any) -> Media:
    (media,) = parse_body(wrap(MEDIA, media_env(**fields))).media
    return media


@pytest.mark.parametrize(
    ("marker", "name"),
    [
        (FORWARD, "fwd"),
        (MEDIA, "media"),
        (ALBUM, "album"),
        (STICKER, "sticker"),
        (CALL, "call"),
        (LINK, "link"),
        (PAY, "pay"),
    ],
)
def test_markers_follow_the_app_format(marker: str, name: str) -> None:
    assert marker == f"\x01pgpchat-{name}-v1\x01"


def test_plain_text_comes_back_as_written() -> None:
    assert parse_body("  ciao\nseconda riga  ") == Body(text="  ciao\nseconda riga  ")


@pytest.mark.parametrize("raw", ["", "   ", "\n\t\n"])
def test_blank_text_is_no_text(raw: str) -> None:
    assert parse_body(raw) == Body(text=None)


def test_body_defaults() -> None:
    body = Body(text="x")
    assert (body.media, body.shape, body.forwarded, body.has_link) == ((), "text", False, False)


def test_photo_with_caption() -> None:
    env = media_env(name="foto.jpg", size=11, cap="guarda che tramonto")
    body = parse_body(wrap(MEDIA, env))
    assert body.shape == "media"
    assert body.text == "guarda che tramonto"
    assert body.media == (
        Media(
            id=MEDIA_ID,
            kind="image",
            mime="image/jpeg",
            url=f"/media/{MEDIA_ID}",
            width=640,
            height=480,
            name="foto.jpg",
            size=11,
            encrypted=True,
        ),
    )
    assert body.media[0].raw == env


@pytest.mark.parametrize("cap", [None, "", 42])
def test_media_without_a_usable_caption_has_no_text(cap: Any) -> None:
    body = parse_body(wrap(MEDIA, media_env(cap=cap)))
    assert body.text is None
    assert len(body.media) == 1


def test_plain_flag_means_the_file_went_up_in_the_clear() -> None:
    assert media_of(plain=True).encrypted is False
    # only a real true: anything else stays sealed
    for value in ("true", 1, False, None):
        assert media_of(plain=value).encrypted is True


@pytest.mark.parametrize(
    "media_id",
    [MEDIA_ID[:-1], MEDIA_ID + "0", MEDIA_ID.replace("0", "g"), MEDIA_ID.replace("-", "_"), 12345, None, ""],
)
def test_media_with_a_bad_id_is_dropped_but_the_caption_stays(media_id: Any) -> None:
    body = parse_body(wrap(MEDIA, media_env(media=media_id, cap="still here")))
    assert body.media == ()
    assert body.text == "still here"
    assert body.shape == "media"


def test_media_id_may_be_upper_case() -> None:
    assert media_of(media=MEDIA_ID.upper()).id == MEDIA_ID.upper()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("image", "image"),
        ("video", "video"),
        ("audio", "audio"),
        ("file", "file"),
        ("sticker", "file"),
        ("IMAGE", "file"),
        (None, "file"),
        (3, "file"),
    ],
)
def test_media_kind_is_one_of_four(kind: Any, expected: str) -> None:
    assert media_of(kind=kind).kind == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), (19999, 19999), (0, None), (-5, None), (20000, None), (True, None), (640.0, None), ("640", None)],
)
def test_media_dimensions_must_be_positive_integers_under_20000(value: Any, expected: int | None) -> None:
    media = media_of(w=value, h=value)
    assert (media.width, media.height) == (expected, expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1500, 1500), (1234.4, 1234), (1234.6, 1235), (0, None), (-1, None), (True, None), ("90", None)],
)
def test_media_duration_is_a_positive_number(value: Any, expected: int | None) -> None:
    assert media_of(kind="audio", dur=value).duration_ms == expected


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (2048, 2048), (-1, None), (True, None), (1.5, None)])
def test_media_size_is_a_non_negative_integer(value: Any, expected: int | None) -> None:
    assert media_of(size=value).size == expected


def test_media_mime_is_cut_at_128_and_strings_only() -> None:
    assert media_of(mime="x" * 300).mime == "x" * 128
    assert media_of(mime=5).mime is None
    assert media_of(mime="").mime is None
    assert media_of(name=["a"]).name is None


@pytest.mark.parametrize("payload", ["{not json", "", "null", "[1, 2]", '"just a string"'])
def test_media_marker_with_a_broken_envelope_yields_nothing(payload: str) -> None:
    body = parse_body(MEDIA + payload)
    assert body.text is None
    assert body.media == ()
    assert not body.forwarded


def album_item(n: int, **fields: Any) -> dict[str, Any]:
    return {"media": f"0f3c5a3e-1111-2222-3333-{n:012d}", "kind": "image", **fields}


def test_album_with_caption() -> None:
    body = parse_body(wrap(ALBUM, {"items": [album_item(1), album_item(2, kind="video")], "cap": "vacanze"}))
    assert body.shape == "album"
    assert body.text == "vacanze"
    assert [m.kind for m in body.media] == ["image", "video"]
    assert [m.url for m in body.media] == [f"/media/{m.id}" for m in body.media]


def test_album_reads_at_most_ten_items() -> None:
    body = parse_body(wrap(ALBUM, {"items": [album_item(n) for n in range(12)]}))
    assert len(body.media) == 10
    assert body.media[-1].id.endswith("000000000009")


def test_album_drops_bad_items_and_never_looks_past_the_tenth() -> None:
    # ten good items in all, but two of the first ten slots are junk: 10 and 11 are never read
    items = [album_item(0), {"media": "bad"}, "junk", *[album_item(n) for n in range(3, 12)]]
    body = parse_body(wrap(ALBUM, {"items": items}))
    assert [m.id[-2:] for m in body.media] == ["00", "03", "04", "05", "06", "07", "08", "09"]


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"items": "nope"}', '{"cap": "only words"}'])
def test_album_without_readable_items(payload: str) -> None:
    body = parse_body(ALBUM + payload)
    assert body.media == ()
    assert body.text == ("only words" if "cap" in payload else None)


def test_forward_unwraps_to_the_original_text() -> None:
    body = parse_body(wrap(FORWARD, {"body": "hello from Anna", "from": "Anna"}))
    assert body == Body(text="hello from Anna", forwarded=True)


def test_link_card_unwraps_to_the_text_with_the_link() -> None:
    body = parse_body(wrap(LINK, {"body": "see https://example.org", "title": "Example"}))
    assert body == Body(text="see https://example.org", has_link=True)


def test_forwarded_photo_keeps_media_and_shape() -> None:
    inner = wrap(MEDIA, media_env(cap="forwarded sunset"))
    body = parse_body(wrap(FORWARD, {"body": inner}))
    assert (body.shape, body.text, body.forwarded, body.has_link) == ("media", "forwarded sunset", True, False)
    assert body.media[0].id == MEDIA_ID


def test_forward_of_a_link_card_carries_both_flags() -> None:
    body = parse_body(wrap(FORWARD, {"body": wrap(LINK, {"body": "https://example.org"})}))
    assert body == Body(text="https://example.org", forwarded=True, has_link=True)


def test_two_levels_of_wrapping_are_read_and_a_third_is_not() -> None:
    two = wrap(FORWARD, {"body": wrap(FORWARD, {"body": "deep"})})
    assert parse_body(two) == Body(text="deep", forwarded=True)
    three = wrap(FORWARD, {"body": two})
    assert parse_body(three) == Body(text=None, shape="unknown", forwarded=True)


@pytest.mark.parametrize("marker", [FORWARD, LINK])
@pytest.mark.parametrize("payload", ["{broken", "[]", '{"body": 5}', '{"text": "no body"}', ""])
def test_wrapper_without_a_readable_body_is_unknown(marker: str, payload: str) -> None:
    assert parse_body(marker + payload) == Body(text=None, shape="unknown")


@pytest.mark.parametrize(("marker", "shape"), [(STICKER, "sticker"), (CALL, "call"), (PAY, "pay")])
def test_shapes_that_carry_no_text(marker: str, shape: str) -> None:
    body = parse_body(marker + '{"cap": "not a caption", "body": "not a body"}')
    assert body == Body(text=None, shape=shape)


@pytest.mark.parametrize(
    "raw",
    ["\x01pgpchat-poll-v1\x01{}", "\x01pgpchat-media-v2\x01{}", "\x01", "\x01anything at all"],
)
def test_unknown_marker_is_unknown_and_never_shown_as_text(raw: str) -> None:
    assert parse_body(raw) == Body(text=None, shape="unknown")


def test_marker_not_at_the_start_is_just_text() -> None:
    raw = "look: " + MEDIA + json.dumps(media_env())
    assert parse_body(raw) == Body(text=raw)


def test_photo_written_by_a_phone(bot_key: BotKey) -> None:
    raw = bot_key.decrypt(fixture_text("msg_media.asc")).data.decode("utf-8")
    body = parse_body(raw)
    assert body.shape == "media"
    assert body.text == "guarda che tramonto"
    (media,) = body.media
    assert (media.id, media.kind, media.mime, media.name, media.size) == (
        MEDIA_ID,
        "image",
        "image/jpeg",
        "foto.jpg",
        11,
    )
    assert (media.width, media.height, media.url, media.encrypted) == (640, 480, f"/media/{MEDIA_ID}", True)


def test_text_written_by_a_phone(bot_key: BotKey) -> None:
    raw = bot_key.decrypt(fixture_text("msg_text_signed.asc")).data.decode("utf-8")
    assert parse_body(raw) == Body(text="Ciao dal telefono: è già sera \U0001f319\nseconda riga")
