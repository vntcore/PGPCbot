"""An in-memory PGP Chat Bot API for the tests.

It answers like the real server where the SDK depends on it - the envelope,
the codes, which body a chat takes, sendId replays, offsets that confirm,
callbacks that can be answered once - and keeps every request so a test can
look at exactly what went over the wire. It serves both the async and the
blocking client from the same state.

Faults are scripted with :meth:`FakeAPI.fail`: an error envelope, a dropped
connection before the request is handled, or a lost answer after it was.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2

BOT_ID = 412
TOKEN = f"pgpbot_{BOT_ID}." + "A" * 43
BASE = "https://api.test"
MEDIA_ID = "0f3c5a3e-1111-2222-3333-444455556666"


def ok(result: Any, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json={"ok": True, "result": result})


def err(status: int, code: str, message: str = "", **extra: Any) -> httpx2.Response:
    headers = {"Retry-After": str(extra["retryAfter"])} if extra.get("retryAfter") else {}
    body = {"ok": False, "error": {"status": status, "code": code, "message": message or code, **extra}}
    return httpx2.Response(status, json=body, headers=headers)


@dataclass
class Fault:
    method: str
    path: re.Pattern[str]
    times: int
    response: Callable[[], httpx2.Response] | None = None
    network: bool = False
    after: bool = False


@dataclass
class FakeChat:
    id: int
    kind: str = "private"
    title: str | None = None
    username: str | None = None
    history_visible: bool = False
    peer: dict[str, Any] | None = None
    keys: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    gone: bool = False
    me: dict[str, Any] = field(default_factory=lambda: {"status": "member", "rights": 255, "restrictedUntil": None})

    def ref(self) -> dict[str, Any]:
        if self.kind == "private":
            return {"id": self.id, "kind": "private"}
        return {"id": self.id, "kind": self.kind, "title": self.title, "username": self.username}


class FakeAPI(httpx2.BaseTransport, httpx2.AsyncBaseTransport):
    """The state of a fake server and the transport that talks to it."""

    def __init__(self, *, token: str = TOKEN) -> None:
        self.token = token
        self.lock = threading.RLock()
        self.me: dict[str, Any] = {
            "id": BOT_ID,
            "username": "testbot",
            "name": "Test",
            "about": None,
            "description": None,
            "mode": "open",
            "fingerprint": None,
            "publicKey": None,
            "privacyMode": True,
            "allowGroups": True,
            "status": "active",
            "queued": 0,
            "limits": {"textMax": 4096, "pollTimeoutMaxS": 50, "sendsPerSecond": 30, "apiCallsPerMinute": 600},
        }
        self.chats: dict[int, FakeChat] = {}
        self.queue: list[dict[str, Any]] = []
        self.next_update_id = 1
        self.messages: dict[int, dict[str, Any]] = {}
        self.next_message_id = 1000
        self.send_ids: dict[str, int] = {}
        self.callbacks: dict[str, dict[str, Any]] = {}
        self.webhook: dict[str, Any] = {"url": None, "secret": None, "failures": 0, "lastError": None}
        self.commands: dict[str, list[dict[str, str]]] = {}
        self.user_keys: dict[int, dict[str, Any]] = {}
        self.handles: dict[str, dict[str, Any]] = {}
        self.media: dict[str, bytes] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: list[Fault] = []
        self.revoked = False
        self.poll_wait = 0.05
        self._arrived = threading.Event()

    # --- scripting ---

    def add_chat(self, chat_id: int, kind: str = "private", **fields: Any) -> FakeChat:
        chat = FakeChat(chat_id, kind, **fields)
        if kind == "private" and chat.peer is None:
            chat.peer = {
                "id": 55,
                "username": "anna",
                "firstName": "Anna",
                "lastName": None,
                "deleted": False,
                "hasKey": True,
            }
        with self.lock:
            self.chats[chat_id] = chat
        return chat

    def add_handle(
        self, username: str, *, user: dict[str, Any] | None = None, chat: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Puts a handle in the pool a lookup searches, held by a person or by a chat."""
        held = user or chat or {}
        result = {
            "kind": "user" if user else str(held.get("kind", "group")),
            "id": held["id"],
            "username": held.get("username", username),
            "user": user,
            "chat": chat,
        }
        with self.lock:
            self.handles[username.lower()] = result
        return result

    def push(self, kind: str, **payload: Any) -> int:
        with self.lock:
            uid = self.next_update_id
            self.next_update_id += 1
            self.queue.append({"updateId": uid, "kind": kind, **payload})
            self._arrived.set()
        return uid

    def push_message(
        self,
        chat_id: int,
        text: str | None = None,
        *,
        armored: str | None = None,
        from_id: int | None = 55,
        message_id: int | None = None,
        edited: bool = False,
        media: list[dict[str, Any]] | None = None,
        reply_to: int | None = None,
    ) -> int:
        chat = self.chats[chat_id]
        message: dict[str, Any] = {
            "id": message_id or self._new_message_id(),
            "chat": chat.ref(),
            "from": None
            if from_id is None
            else {"id": from_id, "username": f"user{from_id}", "firstName": "User", "lastName": None, "isBot": False},
            "date": 1791000000000,
            "replyTo": reply_to,
            "kind": "user",
        }
        if armored is not None:
            message["armored"] = armored
        else:
            message["text"] = text
            message["media"] = media or []
        return self.push("edited_message" if edited else "message", message=message)

    def push_callback(
        self, chat_id: int, message_id: int, data: str, *, from_id: int = 55, expires_in: float = 60
    ) -> str:
        callback_id = uuid.uuid4().hex[:22]
        expires = int((time.time() + expires_in) * 1000)
        with self.lock:
            self.callbacks[callback_id] = {"answered": False, "expiresAt": expires, "answer": None}
        self.push(
            "callback_query",
            callback={
                "id": callback_id,
                "from": {"id": from_id, "username": "anna", "firstName": "Anna", "lastName": None, "isBot": False},
                "chat": self.chats[chat_id].ref(),
                "messageId": message_id,
                "data": data,
                "expiresAt": expires,
            },
        )
        return callback_id

    def fail(
        self,
        method: str,
        path: str,
        *,
        status: int = 500,
        code: str = "internal",
        times: int = 1,
        network: bool = False,
        after: bool = False,
        **extra: Any,
    ) -> None:
        """The next ``times`` matching requests fail.

        ``network``: the connection drops before the server sees the request.
        ``after``: the server handles it, then the answer is lost on the way back.
        Otherwise the server answers with an error envelope.
        """
        response = None if network or after else (lambda: err(status, code, **extra))
        with self.lock:
            self.faults.append(Fault(method.upper(), re.compile(path), times, response, network, after))

    def calls(self, method: str | None = None, path: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [
                r
                for r in self.requests
                if (method is None or r["method"] == method.upper()) and (path is None or re.fullmatch(path, r["path"]))
            ]

    # --- transport ---

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        fault = self._fault_for(request)
        if fault is not None and fault.network:
            raise httpx2.ConnectError("connection refused", request=request)
        response = fault.response() if fault is not None and fault.response else self._route(request)
        if fault is not None and fault.after:
            raise httpx2.ReadTimeout("answer lost", request=request)
        return response

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        path = urlsplit(str(request.url)).path
        if path.endswith("/updates"):
            # a long poll: wait a little for something to arrive, like the real one does
            query = parse_qs(urlsplit(str(request.url)).query)
            timeout = int(query.get("timeout", ["0"])[0])
            if timeout and not self._pending(query):
                self._arrived.clear()
                deadline = time.monotonic() + min(timeout, self.poll_wait)
                # a threading.Event: updates may be pushed from a handler thread (SyncBot)
                while time.monotonic() < deadline and not self._arrived.is_set():  # noqa: ASYNC110
                    await asyncio.sleep(0.005)
        return self.handle_request(request)

    def _pending(self, query: dict[str, list[str]]) -> bool:
        offset = int(query.get("offset", ["0"])[0])
        with self.lock:
            return any(u["updateId"] >= offset for u in self.queue)

    def _fault_for(self, request: httpx2.Request) -> Fault | None:
        path = urlsplit(str(request.url)).path
        with self.lock:
            for fault in self.faults:
                if fault.times > 0 and fault.method == request.method and fault.path.search(path):
                    fault.times -= 1
                    return fault
        return None

    def _new_message_id(self) -> int:
        with self.lock:
            self.next_message_id += 1
            return self.next_message_id

    # --- the server ---

    def _route(self, request: httpx2.Request) -> httpx2.Response:
        url = urlsplit(str(request.url))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                return err(400, "bad_request", "body is not valid json")
        with self.lock:
            self.requests.append(
                {
                    "method": request.method,
                    "path": url.path,
                    "query": query,
                    "json": body,
                    "headers": dict(request.headers),
                }
            )
        if url.path.startswith("/media/"):
            data = self.media.get(url.path.rsplit("/", 1)[-1])
            if data is None:
                return httpx2.Response(404, json={"error": {"status": 404, "code": "media_media_not_found2"}})
            return httpx2.Response(200, content=data, headers={"content-type": "application/octet-stream"})
        if not url.path.startswith("/bot/v1/"):
            return err(404, "not_found", "no such method")
        auth = request.headers.get("authorization", "")
        if self.revoked or auth != f"Bearer {self.token}":
            return err(401, "unauthorized", "missing, revoked or invalid bot token")
        path = url.path[len("/bot/v1") :]
        method = request.method
        paused_ok = {("GET", "/me"), ("GET", "/webhook"), ("DELETE", "/webhook"), ("POST", "/token/revoke")}
        if self.me["status"] != "active" and (method, path) not in paused_ok:
            return err(403, "bot_paused", "this bot is paused by its owner")
        with self.lock:
            return self._dispatch(method, path, query, body if isinstance(body, dict) else {})

    def _dispatch(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> httpx2.Response:
        if (method, path) == ("GET", "/me"):
            me = dict(self.me)
            me.pop("publicKey", None)
            me["webhook"] = {
                "url": self.webhook["url"],
                "hasSecret": self.webhook["secret"] is not None,
                "failures": 0,
                "lastError": None,
            }
            me["queued"] = len(self.queue)
            return ok(me)
        if (method, path) == ("GET", "/updates"):
            if self.webhook["url"]:
                return err(409, "webhook_active", "a webhook is set; delete it before polling")
            offset = int(query.get("offset", 0))
            limit = int(query.get("limit", 100))
            if offset > 0:
                self.queue = [u for u in self.queue if u["updateId"] >= offset]
            return ok([u for u in self.queue if u["updateId"] >= offset][:limit])
        if path == "/webhook":
            if method == "GET":
                return ok(
                    {
                        "url": self.webhook["url"],
                        "hasSecret": self.webhook["secret"] is not None,
                        "pending": len(self.queue),
                        "failures": 0,
                        "lastError": None,
                        "lastOkAt": None,
                        "nextAttemptAt": None,
                    }
                )
            if method == "POST":
                if not str(body.get("url", "")).startswith("https://"):
                    return err(400, "bad_webhook", "webhook url must use https")
                self.webhook.update(url=body["url"], secret=body.get("secret"))
                return ok({"url": body["url"], "hasSecret": body.get("secret") is not None, "pending": len(self.queue)})
            self.webhook.update(url=None, secret=None)
            return ok({"url": None, "pending": len(self.queue)})
        if (method, path) == ("PUT", "/key"):
            if "BEGIN PGP PUBLIC KEY BLOCK" not in str(body.get("publicKey")):
                return err(400, "bad_request", "publicKey: not an armored PGP public key")
            self.me.update(mode="protected", fingerprint=body["fingerprint"].upper(), publicKey=body["publicKey"])
            return ok({"fingerprint": body["fingerprint"].upper(), "mode": "protected"})
        if path == "/commands":
            scope = query.get("scope") or body.get("scope") or "default"
            if method == "GET":
                if "scope" in query:
                    return ok(self.commands.get(scope, []))
                return ok([{"scope": s, "commands": c} for s, c in self.commands.items()])
            if method == "PUT":
                self.commands[scope] = list(body.get("commands", []))
                return ok({"scope": scope, "commands": self.commands[scope]})
            self.commands.pop(scope, None)
            return ok({"scope": scope, "commands": []})
        if (method, path) == ("POST", "/messages"):
            return self._send(body)
        m = re.fullmatch(r"/messages/(\d+)", path)
        if m:
            return (
                self._edit(int(m.group(1)), body) if method == "PATCH" else self._delete(int(m.group(1)), query, body)
            )
        m = re.fullmatch(r"/callbacks/([^/]+)/answer", path)
        if m:
            callback = self.callbacks.get(m.group(1))
            if callback is None:
                return err(404, "callback_not_found", "no such callback for this bot")
            if callback["answered"]:
                return err(409, "callback_answered", "this callback was already answered")
            if callback["expiresAt"] <= time.time() * 1000:
                return err(410, "callback_expired", "this callback is too old to answer")
            callback.update(answered=True, answer=body)
            return ok({"answered": True})
        m = re.fullmatch(r"/chats/(\d+)(/.*)?", path)
        if m:
            return self._chat(method, int(m.group(1)), m.group(2) or "", body)
        m = re.fullmatch(r"/resolve/([^/]+)", path)
        if m:
            name = m.group(1).lower()
            if not re.fullmatch(r"[a-z0-9_]{3,32}", name):
                return err(400, "bad_request", "username: 3-32 characters, letters, digits and underscore")
            found = self.handles.get(name)
            return ok(found) if found else err(404, "username_not_found", "nobody and no chat answers to that name")
        m = re.fullmatch(r"/users/(\d+)/key", path)
        if m:
            key = self.user_keys.get(int(m.group(1)))
            return ok(key) if key else err(404, "user_not_found", "no such user, or no chat in common")
        if (method, path) == ("POST", "/token/revoke"):
            self.revoked = True
            return ok({"revoked": True})
        return err(404, "not_found", "no such method")

    def _mode_of(self, chat: FakeChat) -> str:
        if chat.kind == "private":
            return str(self.me["mode"])
        return "open" if chat.history_visible else "protected"

    def _body(self, chat: FakeChat, body: dict[str, Any], *, optional: bool) -> httpx2.Response | str | None:
        expected = "armored" if self._mode_of(chat) == "protected" else "text"
        text, armored = body.get("text"), body.get("armored")
        if text is None and armored is None:
            return None if optional else err(400, "bad_request", f"{expected} is required", expected=expected)
        if expected == "text" and text is None:
            return err(400, "wrong_body_kind", "this chat is readable: send text, not armored", expected="text")
        if expected == "armored" and armored is None:
            return err(
                400, "wrong_body_kind", "this chat is end-to-end encrypted: send armored, not text", expected="armored"
            )
        return str(text if expected == "text" else armored)

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        chat = self.chats[row["chatId"]]
        view = {
            "id": row["id"],
            "chat": chat.ref(),
            "date": 1791000000000,
            "replyTo": row.get("replyTo"),
            "markup": row.get("markup"),
        }
        view["armored" if row["body"].startswith("-----BEGIN PGP MESSAGE-----") else "text"] = row["body"]
        return view

    def _send(self, body: dict[str, Any]) -> httpx2.Response:
        chat = self.chats.get(body.get("chatId"))  # type: ignore[arg-type]
        if chat is None or chat.gone:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        send_id = body.get("sendId")
        if send_id and send_id in self.send_ids:
            row = self.messages[self.send_ids[send_id]]
            if row["chatId"] != chat.id:
                return err(409, "conflict", "sendId was used in another chat")
            return ok(self._view(row), 200)
        text = self._body(chat, body, optional=False)
        if isinstance(text, httpx2.Response):
            return text
        if chat.kind == "private" and chat.blocked:
            return err(403, "blocked_by_user", "this user blocked the bot")
        row = {
            "id": self._new_message_id(),
            "chatId": chat.id,
            "body": text,
            "replyTo": body.get("replyTo"),
            "markup": body.get("markup"),
        }
        self.messages[row["id"]] = row
        if send_id:
            self.send_ids[send_id] = row["id"]
        return ok(self._view(row), 201)

    def _edit(self, message_id: int, body: dict[str, Any]) -> httpx2.Response:
        chat = self.chats.get(body.get("chatId"))  # type: ignore[arg-type]
        if chat is None:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        row = self.messages.get(message_id)
        if row is None or row["chatId"] != chat.id:
            return err(404, "message_not_found", "no such message in this chat")
        text = self._body(chat, body, optional=True)
        if isinstance(text, httpx2.Response):
            return text
        if text is None and "markup" not in body:
            return err(400, "bad_request", "nothing to change")
        if text is not None:
            row["body"] = text
        if "markup" in body:
            row["markup"] = body["markup"]
        return ok(self._view(row))

    def _delete(self, message_id: int, query: dict[str, str], body: dict[str, Any]) -> httpx2.Response:
        chat_id = int(query.get("chatId") or body.get("chatId") or 0)
        if chat_id not in self.chats:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        row = self.messages.pop(message_id, None)
        if row is None:
            return err(404, "message_not_found", "no such message in this chat")
        return ok({"deleted": True})

    def _chat(self, method: str, chat_id: int, rest: str, body: dict[str, Any]) -> httpx2.Response:
        chat = self.chats.get(chat_id)
        if chat is None or chat.gone:
            return err(404, "chat_not_found", "no such chat, or the bot is not in it")
        mode = self._mode_of(chat)
        if rest == "" and method == "GET":
            view: dict[str, Any] = {**chat.ref(), "mode": mode}
            if chat.kind == "private":
                view.update(user=chat.peer, blocked=chat.blocked)
            else:
                view.update(
                    description=None,
                    memberCount=len(chat.members) or 3,
                    historyVisible=chat.history_visible,
                    slowModeSeconds=0,
                    me=chat.me,
                )
            return ok(view)
        if rest == "/keys":
            if mode == "open":
                return err(400, "chat_is_open", "this chat is readable: send text, no keys are needed")
            if chat.kind == "private" and not chat.keys:
                return err(404, "no_user_key", "this user has not published a key")
            result: dict[str, Any] = {"keys": chat.keys}
            if chat.kind != "private":
                result["memberCount"] = len(chat.keys) + 2
            return ok(result)
        if rest.startswith("/members"):
            if chat.kind == "private":
                return err(400, "bad_request", "a private chat has no members")
            m = re.fullmatch(r"/members/(\d+)", rest)
            if m:
                member = next((x for x in chat.members if x["user"]["id"] == int(m.group(1))), None)
                return ok(member) if member else err(404, "member_not_found", "not a member of this chat")
            return ok({"members": chat.members, "memberCount": len(chat.members)})
        if rest == "/leave":
            if chat.kind == "private":
                return err(400, "bad_request", "a bot cannot leave a private chat")
            chat.gone = True
            return ok({"left": True})
        if rest == "/typing":
            return ok({"sent": True})
        return err(404, "not_found", "no such method")
