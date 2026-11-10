"""One space: its package, its rows and its bot. All state changes happen under `Space.lock`."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Mapping

WEEK_MS = 7 * 24 * 3600 * 1000
PUSH_URL = re.compile(r"https?://[^/\s]+/push/[\w-]+", re.ASCII)


class Store:
    """What a bot gets: the rows the server holds, and a way to send rows and notifications to the phones."""

    def __init__(self, space: "Space") -> None:
        self._space = space

    def rows(self, table: str) -> list[dict]:
        return [r["row"] for r in self._space._table(table).values()]

    def put(self, table: str, row: dict) -> None:
        """Saves a row and sends it to the phones on their next sync."""
        d = self._space.data
        d["seq"] += 1
        self._space._table(table)[str(row["id"])] = {"row": row, "seq": d["seq"], "origin": "server"}

    def notify(self, title: str, body: str) -> None:
        """A notification for every device of this space. Only devices that synced in the last week get it,
        and at most the 20 newest wait per device."""
        self._space._notify(title, body)


def _load_bot(directory: str) -> Any:
    path = os.path.join(directory, "bot.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(f"chameloon_bot_{os.path.basename(directory)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Space:
    def __init__(self, directory: str, bot: Any = None) -> None:
        self.dir = directory
        self.lock = threading.Lock()
        self.bot = bot if bot is not None else _load_bot(directory)
        self.store = Store(self)
        self._file = os.path.join(directory, "server-data.json")
        try:
            with open(self._file, encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {"seq": 0, "seen": [], "devices": {}, "rows": {}}
        if not isinstance(self.data.get("devices"), dict):
            self.data["devices"] = {}  # older files kept a plain list
        self.data.setdefault("notes", {})
        self.data.setdefault("push", {})
        self._seen = set(self.data["seen"])  # the list stays the file format; the set makes the lookup fast

    # -- files

    def _json(self, name: str) -> Any:
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return json.load(f)

    def save(self) -> None:
        tmp = self._file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self._file)

    def _table(self, name: str) -> dict:
        return self.data["rows"].setdefault(name, {})

    def auth_mode(self) -> str | None:
        """The space's access rule from space.json (read each time, so changing it needs no restart)."""
        return self._json("space.json").get("auth")

    def package(self) -> dict:
        meta = self._json("space.json")
        scroll = meta.pop("pageScroll", None) or {}
        meta.pop("$schema", None)
        pages: dict[str, Any] = {}
        pages_dir = os.path.join(self.dir, "pages")
        for f in sorted(os.listdir(pages_dir)):
            if not f.endswith(".oui"):
                continue
            name = f[:-4]
            with open(os.path.join(pages_dir, f), encoding="utf-8") as fh:
                ui = fh.read()
            pages[name] = {"ui": ui, "scroll": scroll[name]} if scroll.get(name) else ui
        return {**meta, "pages": pages}

    # -- notifications

    def _queue(self, device: str, title: str, body: str) -> None:
        queue = self.data["notes"].setdefault(device, [])
        queue.append({"title": title, "body": body})
        self.data["notes"][device] = queue[-20:]

    def _notify(self, title: str, body: str) -> None:
        cutoff = time.time() * 1000 - WEEK_MS
        for device, at in list(self.data["devices"].items()):
            if at < cutoff:
                del self.data["devices"][device]
                self.data["notes"].pop(device, None)
                continue
            url = self.data["push"].get(device)
            if not url:
                self._queue(device, title, body)
                continue
            # Through the relay, so it arrives even with the app closed. If that fails, it waits for the next sync.
            threading.Thread(target=self._relay, args=(device, url, title, body), daemon=True).start()

    def _relay(self, device: str, url: str, title: str, body: str) -> None:
        req = urllib.request.Request(
            url, data=json.dumps({"title": title, "body": body}).encode(), method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                return
        except Exception:
            pass
        with self.lock:
            self.data["push"].pop(device, None)
            self._queue(device, title, body)
            self.save()

    # -- bot calls

    def _call(self, name: str, *args: Any) -> Any:
        fn = getattr(self.bot, name, None)
        return fn(*args) if fn else None

    def has_hook(self) -> bool:
        return callable(getattr(self.bot, "hook", None))

    def hook(self, body: dict, headers: Mapping[str, str]) -> Any:
        answer = self._call("hook", body, self.store, headers)
        self.save()
        return {"ok": True} if answer is None else answer

    def sync(self, device: str, ops: list[dict], cursor: int, push: str | None, phone_version: int | float | None) -> dict:
        d = self.data
        d["devices"][device] = int(time.time() * 1000)
        # shortcut: a real server only posts to the relay it trusts, not to any URL a client sends
        if push and PUSH_URL.fullmatch(push):
            d["push"][device] = push
        else:
            d["push"].pop(device, None)  # notifications off for this space on that phone
        if cursor > d["seq"]:
            cursor = 0  # the server lost its data (or was reset): send everything again
        for o in ops:
            if o["id"] in self._seen:
                continue  # retries repeat op ids
            self._seen.add(o["id"])
            d["seen"].append(o["id"])
            if o["op"] == "event":
                self._call("on_event", o["table"], o["row"], self.store, device)
                continue
            if o["op"] == "delete":
                self._table(o["table"]).pop(str(o["row"]["id"]), None)
            else:
                d["seq"] += 1
                self._table(o["table"])[str(o["row"]["id"])] = {"row": o["row"], "seq": d["seq"], "origin": device}
            self._call("on_op", o, self.store, device)
        self._call("tick", self.store)
        # What changed since this device's cursor, written by the bot or by the user's other devices. Never its own rows.
        pull: dict[str, list] = {}
        for table, rows in d["rows"].items():
            fresh = [r["row"] for r in rows.values() if r["origin"] != device and r["seq"] > cursor]
            if fresh:
                pull[table] = fresh
        notify = d["notes"].pop(device, [])
        self.save()
        # The current package version, so an open app updates the space without waiting for a restart.
        version = self._json("space.json")["version"]
        out: dict[str, Any] = {"done": [o["id"] for o in ops]}
        # A phone on an older version may not have the columns these rows have yet: it updates first (it syncs again
        # right after), and gets everything from the same cursor then. Its changes were taken above either way.
        if phone_version is not None and phone_version < version:
            out.update(pull={}, cursor=cursor, version=version)
        else:
            out.update(pull=pull, cursor=d["seq"], version=version)
        if notify:
            out["notify"] = notify
        return out
