"""Receiving updates on a webhook.

`~pgpcbot.WebhookApp` is a plain ASGI application with no dependency of its own. You get one
from `~pgpcbot.Bot.create_webhook_app`, and serve it with any ASGI server (``uvicorn bot:app``)
or give it a route in FastAPI or Starlette. Each delivery is checked before it is accepted: the
secret header (compared in constant time), the size, and the shape.

When the server gets its ``2xx`` depends on the storage:

- with a durable storage (`~pgpcbot.SQLiteStorage`), as soon as the update is written to it.
  The handlers run afterwards; a crash in between is recovered at the next start;
- with `~pgpcbot.MemoryStorage`, only when the handlers are done, so a crash means the server
  delivers the update again. If they take longer than ``ack_timeout``, the answer is ``503``
  and the server tries again later; the repeat is recognised by its ``updateId`` and does not
  run twice.

Deliveries are at least once either way. The ``updateId`` is what makes a delivery new: one at
or below the highest ``updateId`` accepted is not run again.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets as _secrets
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import _validate as v
from .errors import ConfigurationError, WebhookError

if TYPE_CHECKING:
    from .bot import _BotBase

__all__ = ["SECRET_HEADER", "WebhookApp", "WebhookResult", "generate_secret"]

log = logging.getLogger("pgpcbot.webhook")

SECRET_HEADER = "x-pgpchat-bot-api-secret-token"  # noqa: S105 - a header name
"""The HTTP header that carries the webhook secret, in lower case.

The server sends it as ``X-PGPChat-Bot-Api-Secret-Token`` with every delivery, when the webhook
has a secret. `~pgpcbot.WebhookApp` compares header names without regard to case.
"""
# an update is at most an armored message (128 KiB) plus its envelope; leave headroom
DEFAULT_MAX_BODY = 1024 * 1024

# MutableMapping, as Starlette and FastAPI type them: a dict here would make
# app.mount(..., webhook) an arg-type error for anyone running mypy
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def generate_secret(nbytes: int = 32) -> str:
    """Makes a random secret for a webhook, in the alphabet the server accepts.

    Pass it to `Bot.create_webhook_app`. Make it once and keep it with your other secrets: the
    server sends the secret it was given with each delivery, so every worker process must use
    that same one.

    Args:
        nbytes: How many random bytes. The default, 32, gives 43 characters. The server takes 1
            to 256 characters, so keep it between 1 and 192.

    Returns:
        The secret, made of letters, digits, ``_`` and ``-`` by `secrets.token_urlsafe`.

    Examples:
        The default length:

        >>> len(generate_secret())
        43

        To make one in a terminal::

            python -c "from pgpcbot import generate_secret; print(generate_secret())"
    """
    return _secrets.token_urlsafe(nbytes)


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """The answer `~pgpcbot.WebhookApp.handle` chose, to send back to the server.

    A ``2xx`` status tells the server the update was delivered. Anything else makes it send the
    same update again later, and the updates after it wait.

    Attributes:
        status: The HTTP status code to answer with.
        body: The body, to send as JSON: ``{"ok": True}`` with a ``2xx``, ``{"ok": False}``
            otherwise.
        headers: Extra response headers, as ``(name, value)`` pairs with lower-case names. Empty
            except on a ``401``, which carries ``www-authenticate``.
    """

    status: int
    body: dict[str, Any]
    headers: tuple[tuple[str, str], ...] = ()


def parse_delivery(body: bytes) -> dict[str, Any]:
    """The update in a delivery body, or `WebhookError` with the status to answer."""
    try:
        update = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise WebhookError("the body is not JSON", status=400) from None
    if not isinstance(update, dict):
        raise WebhookError("the body is not a JSON object", status=400)
    uid = update.get("updateId")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise WebhookError("updateId is missing or not an integer", status=400)
    # the rest is the engine's to judge: a shape it can't read ends up in the
    # failed list with its error, where it can be looked at and replayed
    return update


def secret_matches(expected: str | None, given: str | bytes | None) -> bool:
    """Constant time comparison. No expected secret means anything passes."""
    if expected is None:
        return True
    if given is None:
        return False
    if isinstance(given, str):
        given = given.encode("latin-1", "replace")
    return hmac.compare_digest(expected.encode("ascii"), given)


class WebhookApp:
    """The ASGI application the server sends updates to.

    Get one from `Bot.create_webhook_app`. It is a complete ASGI 3 application: it answers HTTP
    requests and takes the lifespan events, so that, served on its own, it starts the bot when
    the web server starts and stops it at shutdown. WebSocket connections are closed.

    Each request is checked in this order, and the status code tells the server what to do:

    - ``404``: a different path, when ``path`` was given;
    - ``405``, with ``Allow: POST``: not a ``POST``;
    - ``401``, with ``WWW-Authenticate``: the secret header is missing or wrong. It is compared
      in constant time, before the body is read;
    - ``413``: the body is larger than ``max_body``;
    - ``408``: the body did not arrive within 10 seconds;
    - ``200``: the update is safe (stored, or handled with `MemoryStorage`), or it is a repeat.
      Also a request with the right secret whose body is not an update: it is logged as an
      error, because a ``4xx`` would make the server send it again for days, and every later
      update would wait behind it;
    - ``503``: the update is not safe yet (it could not be stored, or the handlers took longer
      than ``ack_timeout`` with `MemoryStorage`), or the bot is stopping. The server tries
      again later;
    - ``500``: an unexpected error, logged. The server tries again later.

    Inside another application, the app gets no lifespan events of its own: pass
    ``lifespan=bot.lifespan`` to that application. Without it, the bot starts at the first
    delivery and logs a warning. For a web framework that is not ASGI, `handle` does the checks
    and the processing.

    Attributes:
        url (str | None): The public address the bot registers with the server at every start,
            or ``None``.

    Examples:
        On its own, served with ``uvicorn bot:app``::

            app = bot.create_webhook_app(
                secret=os.environ["WEBHOOK_SECRET"],
                url="https://bot.example.org/pgpchat",
            )

        In FastAPI, next to your own routes::

            from fastapi import FastAPI

            webhook = bot.create_webhook_app(
                secret=os.environ["WEBHOOK_SECRET"], url="https://example.org/pgpchat"
            )
            app = FastAPI(lifespan=bot.lifespan)
            app.add_route("/pgpchat", webhook, methods=["POST"])

        In Starlette, with the same ``webhook``::

            from starlette.applications import Starlette
            from starlette.routing import Route

            app = Starlette(
                routes=[Route("/pgpchat", webhook, methods=["POST"])],
                lifespan=bot.lifespan,
            )

    Warning:
        Give the app a route, not a mount. With the app mounted at ``/pgpchat``, Starlette
        answers a ``POST`` to ``/pgpchat`` with a redirect to ``/pgpchat/``, and the server counts
        a redirect as a failed delivery: the queue stops. If you do mount it, register the
        address with the trailing slash.
    """

    def __init__(
        self,
        bot: _BotBase,
        *,
        secret: str | None,
        path: str | None = None,
        url: str | None = None,
        max_body: int = DEFAULT_MAX_BODY,
        ack_timeout: float = 8.0,
        allow_no_secret: bool = False,
    ) -> None:
        """Sets up the endpoint. Nothing is sent to the server here.

        Use `Bot.create_webhook_app` rather than this: it passes the bot, and `Bot.lifespan`
        then knows which ``url`` to register.

        Args:
            bot (Bot | SyncBot): The bot that runs the updates.
            secret: The secret the server sends in the ``X-PGPChat-Bot-Api-Secret-Token``
                header, checked on every delivery: 1 to 256 characters of ``A-Z``, ``a-z``,
                ``0-9``, ``_`` and ``-``. `generate_secret` makes a good one. ``None`` only
                with ``allow_no_secret=True``.
            path: Answer only on this path, and ``404`` elsewhere. A trailing ``/`` makes no
                difference. Leave it out when another application routes to the app.
            url: The app's public ``https`` address. When given, the bot registers it with the
                server, with the secret, every time it starts.
            max_body: The largest body accepted, in bytes: 1 MiB by default. A larger one gets
                ``413``.
            ack_timeout: With `MemoryStorage`, how many seconds to wait for the handlers before
                answering ``503``. Above 0 and under 10, the server's own time limit.
            allow_no_secret: Accept ``secret=None``.

        Raises:
            ConfigurationError: ``secret`` is ``None`` without ``allow_no_secret=True``, or
                ``ack_timeout`` is not above 0 and under 10.
            ValidationError: ``secret`` or ``url`` is malformed.

        Warning:
            With ``allow_no_secret=True`` and no secret, anyone who finds the address can send
            the bot fake updates. Use it only when something in front of the app already checks
            that requests come from the server.
        """
        if secret is None and not allow_no_secret:
            raise ConfigurationError(
                "a webhook without a secret takes updates from anybody who finds the URL. "
                "Pass secret=generate_secret() (and the same to set_webhook), or "
                "allow_no_secret=True if something in front of it already authenticates the server"
            )
        if secret is not None:
            v.check_secret(secret)
        if url is not None:
            v.check_webhook_url(url)
        if not 0 < ack_timeout < 10:
            raise ConfigurationError("ack_timeout must be under the server's 10 second limit")
        self._bot = bot
        self._secret = secret
        self._path = (path.rstrip("/") or "/") if path else None
        self.url = url
        self._max_body = max_body
        self._ack_timeout = ack_timeout
        self._read_timeout = 10.0

    def __repr__(self) -> str:
        return f"<WebhookApp path={self._path or '*'} secret={'set' if self._secret else 'none'}>"

    @property
    def secret(self) -> str | None:
        """The secret checked on every delivery, or ``None`` if the app was made without one."""
        return self._secret

    # --- the framework-free core ---

    async def handle(self, headers: Mapping[str, str], body: bytes, *, peer: str | None = None) -> WebhookResult:
        """Checks and processes one delivery, for a web framework that is not ASGI.

        It checks the secret, the size and the shape, then hands the update to the bot and
        answers as the ASGI app does. The path, the method and the 10-second limit for reading
        the body are left to your framework. Start the bot before the first
        delivery (``await bot.start()``) and stop it at the end (``await bot.stop()``).

        Args:
            headers: The request headers. Names are matched without regard to case.
            body: The raw request body.
            peer: The client's address, only used in log messages.

        Returns:
            The status, body and headers to answer with. Anything but a ``2xx`` makes the
            server send the update again later.

        Example:
            A route in aiohttp::

                from aiohttp import web


                async def pgpchat(request: web.Request) -> web.Response:
                    body = await request.read()
                    result = await webhook.handle(request.headers, body, peer=request.remote)
                    return web.json_response(
                        result.body, status=result.status, headers=dict(result.headers)
                    )
        """
        given = next((value for key, value in headers.items() if key.lower() == SECRET_HEADER), None)
        if not secret_matches(self._secret, given):
            return self._refused(given, peer)
        if len(body) > self._max_body:
            return WebhookResult(413, {"ok": False})
        try:
            update = parse_delivery(body)
        except WebhookError as exc:
            # it carried the right secret, so it is the server talking. a 4xx would
            # have it retry this one for days, holding every later update behind it
            log.error("webhook delivery with a valid secret dropped, %d bytes: %s", len(body), exc)
            return WebhookResult(200, {"ok": True})
        status = await self._bot._accept_delivery(update, ack_timeout=self._ack_timeout)
        return WebhookResult(status, {"ok": 200 <= status < 300})

    @staticmethod
    def _refused(given: str | None, peer: str | None) -> WebhookResult:
        # nothing more in the answer: a probe learns nothing from it
        log.warning(
            "webhook delivery refused: %s secret%s",
            "wrong" if given else "missing",
            f" from {peer}" if peer else "",
        )
        return WebhookResult(401, {"ok": False}, (("www-authenticate", "PGPChat-Secret-Token"),))

    # --- ASGI ---

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serves one ASGI connection: an HTTP request, or the lifespan events of the server.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI function that receives the request's messages.
            send: The ASGI function that sends the answer.
        """
        kind = scope.get("type")
        if kind == "lifespan":
            await self._lifespan(receive, send)
            return
        if kind != "http":
            # a websocket or anything else: not ours
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1003})
            return

        if self._path is not None and _route_path(scope) != self._path:
            await _respond(send, 404, {"ok": False})
            return
        if scope.get("method") != "POST":
            await _respond(send, 405, {"ok": False}, extra=[(b"allow", b"POST")])
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = scope.get("client")
        peer = f"{client[0]}:{client[1]}" if isinstance(client, (list, tuple)) and len(client) == 2 else None
        if not secret_matches(self._secret, headers.get(SECRET_HEADER)):
            # before reading the body: without the secret nobody gets a worker and a buffer for 10 s
            result = self._refused(headers.get(SECRET_HEADER), peer)
            await _respond(
                send, result.status, result.body, extra=[(k.encode(), v.encode()) for k, v in result.headers]
            )
            return
        declared = headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > self._max_body:
            await _respond(send, 413, {"ok": False})
            return
        try:
            # a sender that trickles the body in can't hold a worker for ever
            async with asyncio.timeout(self._read_timeout):
                body = await _read_body(receive, self._max_body)
        except TimeoutError:
            await _respond(send, 408, {"ok": False})
            return
        if body is None:
            await _respond(send, 413, {"ok": False})
            return
        try:
            result = await self.handle(headers, body, peer=peer)
        except Exception:
            log.exception("webhook delivery failed")
            result = WebhookResult(500, {"ok": False})
        await _respond(send, result.status, result.body, extra=[(k.encode(), v.encode()) for k, v in result.headers])

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self._bot._start_webhook(self)
                except Exception as exc:
                    log.exception("the bot could not start")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await self._bot._shutdown()
                finally:
                    await send({"type": "lifespan.shutdown.complete"})
                return


def _route_path(scope: Scope) -> str:
    path = scope.get("path") or "/"
    root = scope.get("root_path") or ""
    # Starlette's Mount keeps the full path and sets root_path; plain servers don't
    if root and path.startswith(root):
        path = path[len(root) :] or "/"
    return path.rstrip("/") or "/"


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks)
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _respond(
    send: Send, status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
        (b"cache-control", b"no-store"),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
