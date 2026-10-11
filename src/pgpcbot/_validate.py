"""Client side copies of the server's rules.

Each check here mirrors one in the server's code, so a request that passes
it is not refused for that reason. Lengths are counted in UTF-16 code units
because that is what JavaScript's `.length` counts: an emoji outside the
BMP is one character to Python and two to the server.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import SplitResult, urlsplit

from .errors import ValidationError

TEXT_MAX = 4096
ARMORED_MAX = 128 * 1024
CALLBACK_ANSWER_MAX = 200
URL_MAX = 1024
PUBLIC_KEY_MAX = 64 * 1024
COMMANDS_PER_SCOPE = 100
COMMAND_DESCRIPTION_MAX = 256
POLL_LIMIT_MAX = 100
POLL_TIMEOUT_MAX = 50

ARMOR_MESSAGE = "-----BEGIN PGP MESSAGE-----"
ARMOR_PUBLIC_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
ARMOR_PUBLIC_END = "-----END PGP PUBLIC KEY BLOCK-----"

# \p{Cc}: C0, DEL and C1. Messages may keep \t and \n, nothing else may.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_IN_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# what String.prototype.trim() removes, which is not quite str.strip(): JS also
# drops U+FEFF, python drops a few C0 separators JS keeps
JS_SPACE = "".join(
    map(
        chr,
        (
            0x20,
            0x09,
            0x0A,
            0x0B,
            0x0C,
            0x0D,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
            0xFEFF,
        ),
    )
)
_JS_SPACE_RUN = re.compile("[" + re.escape(JS_SPACE) + "]+")

# use these with fullmatch: python's $ also matches before a final newline and
# \d takes any unicode digit, where the server's javascript does neither
COMMAND_RE = re.compile(r"[a-z0-9_]{1,32}")
SCOPE_RE = re.compile(r"default|private|groups|groupAdmins|chat:[0-9]{1,12}")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
HANDLE_RE = re.compile(r"[A-Za-z0-9_]{3,32}")
FINGERPRINT_RE = re.compile(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")
# what new URL() strips from both ends before it parses
_URL_EDGES = "".join(map(chr, range(0x21)))


def utf16_len(text: str) -> int:
    # surrogatepass: a lone surrogate (say, from a surrogateescape'd file name) is one unit to JS too
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def js_trim(text: str) -> str:
    return text.strip(JS_SPACE)


def has_control(text: str) -> bool:
    return _CONTROL.search(text) is not None


def normalize_newlines(text: str) -> str:
    # the server refuses \r outright, and a bot that reads a file written on
    # windows would otherwise get a 400 it can't make sense of
    return text.replace("\r\n", "\n").replace("\r", "\n")


def check_id(value: object, field: str) -> int:
    # bool is an int subclass: chat_id=True must not become chat 1
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field} must be a positive integer, got {value!r}", field=field)
    return value


def check_text(text: object, *, field: str = "text", limit: int = TEXT_MAX) -> str:
    if not isinstance(text, str):
        raise ValidationError(f"{field} must be a string, got {type(text).__name__}", field=field)
    text = normalize_newlines(text)
    if not js_trim(text):
        raise ValidationError(f"{field} must not be empty", field=field)
    if utf16_len(text) > limit:
        raise ValidationError(
            f"{field} is {utf16_len(text)} characters long (UTF-16), the limit is {limit}", field=field
        )
    found = _CONTROL_IN_TEXT.search(text)
    if found:
        raise ValidationError(f"{field} contains the control character U+{ord(found.group()):04X}", field=field)
    return text


def check_armored(armored: object, *, field: str = "armored", limit: int = ARMORED_MAX) -> str:
    if not isinstance(armored, str) or ARMOR_MESSAGE not in armored:
        raise ValidationError(f"{field} must be an ASCII-armored PGP MESSAGE", field=field)
    if len(armored) > limit:
        raise ValidationError(f"{field} is {len(armored)} bytes, the limit is {limit}", field=field)
    return armored


def check_send_id(value: object) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    raise ValidationError(f"send_id must be a UUID, got {value!r}", field="send_id")


def _split_http_url(url: object, *, field: str, limit: int) -> SplitResult:
    if not isinstance(url, str) or not url:
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    if utf16_len(url) > limit:
        raise ValidationError(f"{field} is longer than {limit} characters", field=field)
    # the server parses with new URL(), which forgives more than urlsplit: it trims
    # spaces and controls at the ends, drops tabs and newlines, and for http(s)
    # takes any run of / or \ after the colon as the start of the host
    # ("https:example.org" is fine). Refuse only what it refuses for sure
    cleaned = url.strip(_URL_EDGES).replace("\t", "").replace("\n", "").replace("\r", "")
    scheme, colon, rest = cleaned.partition(":")
    if not colon or scheme.lower() not in ("http", "https"):
        raise ValidationError(f"{field} must be an absolute http or https URL", field=field)
    try:
        parts = urlsplit(f"{scheme.lower()}://{rest.replace(chr(92), '/').lstrip('/')}")
        port = parts.port  # raises on a junk port
    except ValueError as exc:
        raise ValidationError(f"{field} is not a valid URL: {exc}", field=field) from None
    del port
    if not parts.hostname:
        raise ValidationError(f"{field} must be an absolute http or https URL", field=field)
    if parts.username or parts.password:
        raise ValidationError(f"{field} must not carry credentials", field=field)
    return parts


def check_http_url(url: object, *, field: str, limit: int = URL_MAX) -> str:
    """An absolute http(s) URL with no user or password in it, as `new URL()` on the server sees it."""
    _split_http_url(url, field=field, limit=limit)
    assert isinstance(url, str)
    return url


def check_webhook_url(url: object) -> str:
    if _split_http_url(url, field="url", limit=URL_MAX).fragment:
        raise ValidationError("url must not carry a fragment", field="url")
    assert isinstance(url, str)
    # https is the server's call: a server set up for local development can allow http,
    # and anything else is refused with bad_webhook
    return url


def check_secret(secret: object) -> str:
    if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
        raise ValidationError("secret must be 1-256 characters of A-Z, a-z, 0-9, _ and -", field="secret")
    return secret


def check_scope(scope: object) -> str:
    if scope is None or scope == "":
        return "default"
    if isinstance(scope, str) and SCOPE_RE.fullmatch(scope):
        return scope
    raise ValidationError(
        f"unknown scope {scope!r}: use default, private, groups, groupAdmins or chat:<id>",
        field="scope",
    )


def clean_command(command: object) -> str:
    raw = command if isinstance(command, str) else ""
    name = js_trim(raw).lstrip("/").lower()
    if not COMMAND_RE.fullmatch(name):
        raise ValidationError(
            f"invalid command {raw!r}: 1-32 lowercase letters, digits and underscores",
            field="commands",
        )
    return name


def clean_command_description(description: object, command: str) -> str:
    # the server folds every run of whitespace into one space before it
    # measures, so do the same or the length check disagrees with it
    text = js_trim(_JS_SPACE_RUN.sub(" ", description if isinstance(description, str) else ""))
    if not 1 <= utf16_len(text) <= COMMAND_DESCRIPTION_MAX:
        raise ValidationError(
            f"description of /{command} must be 1-{COMMAND_DESCRIPTION_MAX} characters",
            field="commands",
        )
    if has_control(text):
        raise ValidationError(f"description of /{command} contains control characters", field="commands")
    return text


def check_username(value: object) -> str:
    # people write handles with the @ on, because that is how the app shows
    # them, and the path wants it off
    raw = value if isinstance(value, str) else ""
    name = js_trim(raw).lstrip("@")
    if not HANDLE_RE.fullmatch(name):
        raise ValidationError(
            f"invalid username {raw!r}: 3-32 characters, letters, digits and underscore",
            field="username",
        )
    return name


def check_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not FINGERPRINT_RE.fullmatch(value.strip()):
        raise ValidationError("fingerprint must be 40 (v4) or 64 (v6) hex characters", field="fingerprint")
    return value.strip().upper()


def check_public_key(value: object) -> str:
    if not isinstance(value, str) or ARMOR_PUBLIC_BEGIN not in value or ARMOR_PUBLIC_END not in value:
        raise ValidationError("public_key must be an ASCII-armored PGP PUBLIC KEY BLOCK", field="public_key")
    if len(value) > PUBLIC_KEY_MAX:
        raise ValidationError(f"public_key is longer than {PUBLIC_KEY_MAX} characters", field="public_key")
    if "PRIVATE KEY" in value:
        # never, under any circumstance, let a private block leave the process
        raise ValidationError("that looks like a private key: only the public key is published", field="public_key")
    return value


def check_poll(offset: object, limit: object, timeout: object) -> dict[str, int]:
    params: dict[str, int] = {}
    if offset is not None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an integer >= 0", field="offset")
        params["offset"] = offset
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= POLL_LIMIT_MAX:
            raise ValidationError(f"limit must be between 1 and {POLL_LIMIT_MAX}", field="limit")
        params["limit"] = limit
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= POLL_TIMEOUT_MAX:
            raise ValidationError(f"timeout must be between 0 and {POLL_TIMEOUT_MAX} seconds", field="timeout")
        params["timeout"] = timeout
    return params


def check_callback_answer(text: object, url: object) -> tuple[str | None, str | None]:
    clean: str | None = None
    if text is not None:
        if not isinstance(text, str):
            raise ValidationError("text must be a string", field="text")
        if utf16_len(text) > CALLBACK_ANSWER_MAX:
            raise ValidationError(f"text is longer than {CALLBACK_ANSWER_MAX} characters", field="text")
        clean = js_trim(text) or None
        # here even a newline is refused: the answer is one line flashed over the chat
        if clean and has_control(clean):
            raise ValidationError("text must be a single line with no control characters", field="text")
    # the server reads an empty url as none at all
    link = check_http_url(url, field="url") if url is not None and url != "" else None
    return clean, link
