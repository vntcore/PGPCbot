"""Reads what a decrypted message body actually is.

Internal. Inside the armor the apps do not always write plain words. A photo, an album, a
sticker, a forward, a link card, a call notice or a payment is a marker
(``U+0001 pgpchat-<shape>-v1 U+0001``) followed by JSON: the app's ``MessageWire`` format. The
server reads the same shapes out of readable chats before it hands them to a bot; this module
does it for encrypted ones, with the same rules, so a handler sees ``text`` and ``media`` the
same way in both kinds of chat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import Media

__all__ = ["Body", "parse_body"]


def _mark(name: str) -> str:
    return f"\x01pgpchat-{name}-v1\x01"


FORWARD = _mark("fwd")
MEDIA = _mark("media")
ALBUM = _mark("album")
STICKER = _mark("sticker")
CALL = _mark("call")
LINK = _mark("link")
PAY = _mark("pay")

_MEDIA_ID_CHARS = frozenset("0123456789abcdefABCDEF-")


@dataclass(frozen=True, slots=True)
class Body:
    """A decrypted body, understood.

    Attributes:
        text: The words: the plain text, or the caption of a photo or an album. ``None`` when
            there are none, as for a sticker, a call notice, a payment, or a body of white space.
        media: The attachments: one for ``"media"``, up to 10 for ``"album"``. An entry that
            can't be read is left out.
        shape: What the body is: ``"text"``, ``"media"``, ``"album"``, ``"sticker"``,
            ``"call"``, ``"pay"`` or ``"unknown"`` (a shape this version can't read). A forward
            or a link card has the shape of what it wraps.
        forwarded: ``True`` when the body came wrapped as a forward.
        has_link: ``True`` when the body came wrapped in a link card.
    """

    text: str | None
    media: tuple[Media, ...] = ()
    shape: str = "text"
    forwarded: bool = False
    has_link: bool = False


def _json_after(raw: str, prefix: str) -> Any:
    try:
        return json.loads(raw[len(prefix) :])
    except ValueError:
        return None


def _media_of(env: Any) -> Media | None:
    if not isinstance(env, dict):
        return None
    media_id = env.get("media")
    if not isinstance(media_id, str) or len(media_id) != 36 or not set(media_id) <= _MEDIA_ID_CHARS:
        return None
    raw_kind = env.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind in ("image", "video", "audio", "file") else "file"

    def _pos_int(key: str, top: int) -> int | None:
        value = env.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < top:
            return None
        return value

    duration = env.get("dur")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    size = env.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        size = None
    mime = env.get("mime") if isinstance(env.get("mime"), str) else None
    name = env.get("name") if isinstance(env.get("name"), str) else None
    return Media(
        id=media_id,
        kind=kind,
        mime=mime[:128] if mime else None,
        url=f"/media/{media_id}",
        width=_pos_int("w", 20000),
        height=_pos_int("h", 20000),
        duration_ms=round(duration) if duration is not None else None,
        name=name,
        size=size,
        # plain means it went up in the clear; everything else is sealed
        encrypted=env.get("plain") is not True,
        raw=env,
    )


def parse_body(raw: str, *, _depth: int = 0) -> Body:
    """Reads one decrypted body. It never raises: a shape it can't read is ``"unknown"``.

    Args:
        raw: The decrypted text.
        _depth: How deep inside forwards and link cards this call is. Callers leave it at 0.

    Returns:
        The body, with its text, its media and its shape.
    """
    if _depth > 2:
        return Body(text=None, shape="unknown")

    if raw.startswith((FORWARD, LINK)):
        is_forward = raw.startswith(FORWARD)
        env = _json_after(raw, FORWARD if is_forward else LINK)
        if not isinstance(env, dict) or not isinstance(env.get("body"), str):
            return Body(text=None, shape="unknown")
        inner = parse_body(env["body"], _depth=_depth + 1)
        return Body(
            text=inner.text,
            media=inner.media,
            shape=inner.shape,
            forwarded=inner.forwarded or is_forward,
            has_link=inner.has_link or not is_forward,
        )

    if raw.startswith(MEDIA):
        env = _json_after(raw, MEDIA)
        media = _media_of(env)
        caption = env.get("cap") if isinstance(env, dict) and isinstance(env.get("cap"), str) else None
        return Body(text=caption or None, media=(media,) if media else (), shape="media")

    if raw.startswith(ALBUM):
        env = _json_after(raw, ALBUM)
        items = env.get("items") if isinstance(env, dict) else None
        found = tuple(m for m in (_media_of(i) for i in (items if isinstance(items, list) else [])[:10]) if m)
        caption = env.get("cap") if isinstance(env, dict) and isinstance(env.get("cap"), str) else None
        return Body(text=caption or None, media=found, shape="album")

    if raw.startswith(STICKER):
        return Body(text=None, shape="sticker")
    if raw.startswith(CALL):
        return Body(text=None, shape="call")
    if raw.startswith(PAY):
        return Body(text=None, shape="pay")

    # any other control character up front is a shape a newer app writes
    if raw.startswith("\x01"):
        return Body(text=None, shape="unknown")

    return Body(text=raw if raw.strip() else None)
