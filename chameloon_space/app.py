"""The server: a framework-free handler, plus WSGI, ASGI and a stdlib HTTP server around it.

Routes per space (folder <root>/<name>/ with space.json): /<name>/space, /<name>/sync, /<name>/img/<file>, /<name>/hook.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import threading
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote

from .auth import ReplayCache, check_access
from .space import Space

IMAGE = re.compile(r"[\w-]+\.(png|jpe?g|svg)", re.ASCII)
NAME = re.compile(r"[a-z0-9-]+", re.ASCII)
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}




class Headers(Mapping):
    """Request headers, looked up without regard to case."""

    def __init__(self, items: Mapping[str, str] | Iterable[tuple[str, str]] = ()) -> None:
        pairs = items.items() if isinstance(items, Mapping) else items
        self._d = {k.lower(): v for k, v in pairs}

    def __getitem__(self, key: str) -> str:
        return self._d[key.lower()]

    def __iter__(self):
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)


def _json(status: int, value: Any) -> tuple[int, list[tuple[str, str]], bytes]:
    return status, [("Content-Type", "application/json")], json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _text(status: int, text: str) -> tuple[int, list[tuple[str, str]], bytes]:
    return status, [("Content-Type", "text/plain; charset=utf-8")], text.encode()


def _number(value: Any) -> int:
    """Cursors arrive as numbers or numeric strings; anything else counts as 0."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return int(n) if n == n and abs(n) != float("inf") else 0


class App:
    """Serves every space folder in `root`. With `token`, every request needs `Authorization: Bearer <token>`.

    `bots` maps a space name to a bot object (any object with some of tick, on_op, on_event, hook); it wins over
    the space's bot.py. An App is a WSGI app and an ASGI app: mount it in Flask, Django, FastAPI or Starlette.
    """

    def __init__(self, root: str, token: str | None = None, bots: Mapping[str, Any] | None = None) -> None:
        self.root = os.path.abspath(root)
        self.token = token
        self.bots = dict(bots or {})
        self.replays = ReplayCache()
        self._spaces: dict[str, Space] = {}
        self._make = threading.Lock()

    def space(self, name: str) -> Space | None:
        if not NAME.fullmatch(name) or not os.path.exists(os.path.join(self.root, name, "space.json")):
            return None
        with self._make:
            if name not in self._spaces:
                self._spaces[name] = Space(os.path.join(self.root, name), self.bots.get(name))
            return self._spaces[name]

    def handle(self, method: str, target: str, headers: Mapping[str, str] | Iterable[tuple[str, str]], body: bytes = b"", mount: str = ""):
        """(method, path?query, headers, body) -> (status, [(header, value)], body).

        `target` is exactly what the client requested, because the phone signs it. When the app is mounted under
        a prefix, pass it as `mount`: it stays in the signed path but is dropped for routing.
        """
        headers = headers if isinstance(headers, Headers) else Headers(headers)
        method = method.upper()
        if self.token and not hmac.compare_digest(headers.get("authorization", "").encode(), f"Bearer {self.token}".encode()):
            return _text(401, "unauthorized")
        path = target.split("?", 1)[0]
        if mount and path.startswith(mount):
            path = path[len(mount):]
        parts = path.split("/")
        parts += [""] * (4 - len(parts))
        name, route, file = parts[1], parts[2], parts[3]
        if not name:
            return _json(200, sorted(n for n in os.listdir(self.root) if self.space(n)))
        s = self.space(name)
        if s is None:
            return _text(404, "no such space")
        # the space's own access rule (auth.py); hooks come from other services and check their caller in the bot
        if route != "hook":
            denied = check_access(s.dir, s.auth_mode(), method, target, headers, body, self.replays, s.lock)
            if denied:
                return _json(denied[0], {"error": denied[1]})
        if method == "GET" and route == "space":
            return _json(200, s.package())
        if method == "POST" and route == "sync":
            return self._sync(s, body)
        if method == "POST" and route == "hook":
            try:
                payload = json.loads(body or b"null")
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return _text(400, "hook needs a JSON object")
            if not s.has_hook():
                return _text(404, "this space has no hook")
            with s.lock:
                return _json(200, s.hook(payload, headers))
        if method == "GET" and route == "img" and IMAGE.fullmatch(file):
            path = os.path.join(s.dir, "img", file)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return 200, [("Content-Type", TYPES[os.path.splitext(file)[1]])], f.read()
        return _text(404, "not found")

    def _sync(self, s: Space, body: bytes):
        try:
            req = json.loads(body or b"null") or {}
        except ValueError:
            return _text(400, "sync needs a JSON body")
        if not isinstance(req, dict):
            return _text(400, "sync needs a JSON body")
        device = req.get("device")
        if not isinstance(device, str) or not device:
            return _text(400, "sync needs a device id")
        ops = req.get("ops") or []
        # A malformed op would half-apply a sync, so refuse the whole request before touching anything.
        if not isinstance(ops, list) or not all(
            isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("table"), str)
            and (o.get("op") == "event" or (isinstance(o.get("row"), dict) and "id" in o["row"]))
            for o in ops
        ):
            return _text(400, "sync got a malformed op")
        push = req.get("push")
        version = req.get("version")
        with s.lock:
            return _json(200, s.sync(
                device, ops, _number(req.get("cursor")),
                push if isinstance(push, str) else None,
                version if isinstance(version, (int, float)) and not isinstance(version, bool) else None,
            ))

    # -- WSGI

    def wsgi(self, environ, start_response):
        mount = quote(environ.get("SCRIPT_NAME", ""))
        target = mount + quote(environ.get("PATH_INFO", ""), safe="/%:@!$&'()*+,;=~")
        if environ.get("QUERY_STRING"):
            target += "?" + environ["QUERY_STRING"]
        headers = [(k[5:].replace("_", "-"), v) for k, v in environ.items() if k.startswith("HTTP_")]
        headers += [(k.replace("_", "-"), environ[k]) for k in ("CONTENT_TYPE", "CONTENT_LENGTH") if environ.get(k)]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        status, out, payload = self.handle(environ["REQUEST_METHOD"], target, headers, body, mount)
        start_response(f"{status} {HTTPStatus(status).phrase}", [*out, ("Content-Length", str(len(payload)))])
        return [payload]

    # -- ASGI

    async def asgi(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        mount = scope.get("root_path", "")
        # raw_path is what the client sent (percent-encoded); path is already decoded
        raw = scope.get("raw_path")
        path = raw.decode("latin-1") if raw else quote(scope["path"])
        if mount and not path.startswith(mount):
            path = mount + path  # servers that report raw_path relative to the mount
        target = path + ("?" + scope["query_string"].decode("latin-1") if scope.get("query_string") else "")
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        # file and JSON work blocks, so it runs in a thread, not on the event loop
        status, out, payload = await asyncio.to_thread(self.handle, scope["method"], target, headers, body, mount)
        out = [(k.lower().encode(), v.encode()) for k, v in out] + [(b"content-length", str(len(payload)).encode())]
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": payload})

    def __call__(self, *args):
        """WSGI (environ, start_response) or ASGI (scope, receive, send), whichever the server calls with."""
        return self.wsgi(*args) if len(args) == 2 else self.asgi(*args)


def create_app(root: str, token: str | None = None, bots: Mapping[str, Any] | None = None) -> App:
    return App(root, token, bots)


def create_handler(root: str, token: str | None = None, bots: Mapping[str, Any] | None = None):
    """The framework-free core: handler(method, target, headers, body) -> (status, headers, body)."""
    return App(root, token, bots).handle


def make_server(app: App, port: int = 8787, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class RequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, headers, payload = app.handle(self.command, self.path, self.headers.items(), body)
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True
    return server


def serve(root: str, port: int = 8787, token: str | None = None, bots: Mapping[str, Any] | None = None, host: str = "0.0.0.0") -> None:
    """Serves the spaces in `root` over HTTP until interrupted."""
    app = App(root, token, bots)
    names = sorted(n for n in os.listdir(app.root) if os.path.exists(os.path.join(app.root, n, "space.json")))
    server = make_server(app, port, host)
    print(f"Serving {len(names)} space(s) from {app.root} on port {server.server_address[1]}{' (token required)' if token else ''}:", flush=True)
    for n in names:
        print(f"  /{n}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
