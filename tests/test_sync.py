import json

from chameloon_space import create_app, create_handler

from conftest import make_space


def sync(h, space, device, ops=(), cursor=0, **extra):
    code, _, body = h("POST", f"/{space}/sync", {}, json.dumps({"device": device, "ops": list(ops), "cursor": cursor, **extra}).encode())
    assert code == 200
    return json.loads(body)


def op(i, table, row, kind="upsert"):
    return {"id": i, "op": kind, "table": table, "row": row, "at": "now"}


def test_pull_excludes_own_rows_and_cursor_advances(root):
    h = create_handler(str(root))
    a = sync(h, "open", "a", [op("1", "notes", {"id": "n", "text": "hi"})])
    assert a["done"] == ["1"] and a["pull"] == {} and a["version"] == 1
    b = sync(h, "open", "b")
    assert b["pull"]["notes"] == [{"id": "n", "text": "hi"}]
    assert sync(h, "open", "b", cursor=str(b["cursor"]))["pull"] == {}  # numeric strings work


def test_retries_deletes_and_lost_data(root):
    h = create_handler(str(root))
    o = op("1", "notes", {"id": "n", "text": "one"})
    sync(h, "open", "a", [o])
    b = sync(h, "open", "b")
    sync(h, "open", "a", [o])  # retry
    assert sync(h, "open", "b", cursor=b["cursor"])["pull"] == {}
    sync(h, "open", "a", [op("2", "notes", {"id": "n"}, "delete")])
    assert sync(h, "open", "c")["pull"] == {}  # the only row was deleted
    sync(h, "open", "a", [op("3", "notes", {"id": "m", "text": "kept"})])
    assert sync(h, "open", "d", cursor=10**9)["pull"]["notes"][0]["id"] == "m"


def test_old_phone_version(root):
    h = create_handler(str(root))
    make_space(root, "v2", version=2)
    sync(h, "v2", "a", [op("1", "notes", {"id": "n"})])
    old = sync(h, "v2", "b", [op("2", "notes", {"id": "o"})], version=1)
    assert old["pull"] == {} and old["cursor"] == 0 and old["version"] == 2 and old["done"] == ["2"]
    assert len(sync(h, "v2", "b", cursor=old["cursor"], version=2)["pull"]["notes"]) == 1


def test_bad_requests(root):
    h = create_handler(str(root))
    assert h("POST", "/open/sync", {}, b'{"ops": []}')[0] == 400
    assert h("POST", "/open/sync", {}, b'{"device": ""}')[0] == 400
    assert h("POST", "/open/sync", {}, b"nope")[0] == 400
    assert h("POST", "/open/sync", {}, b'{"device": "a", "ops": [{"id": 1}]}')[0] == 400
    assert h("GET", "/Open/space", {})[0] == 404
    assert h("GET", "/open/img/..%2Fspace.json", {})[0] == 404


def test_bot_py_hook_notify_and_state_file(root):
    make_space(root, "bot", bot=(
        "def on_op(op, store, device):\n"
        "    store.put('seen', {'id': op['id'], 'by': device})\n"
        "def on_event(event, data, store, device):\n"
        "    store.notify(event, data['x'])\n"
        "def tick(store):\n"
        "    store.put('ticks', {'id': 't', 'n': len(store.rows('seen'))})\n"
        "def hook(body, store, headers):\n"
        "    return {'who': headers['X-Who'], 'got': body}\n"
    ))
    h = create_handler(str(root))
    sync(h, "bot", "a")  # the phone is known now, so notify reaches it
    r = sync(h, "bot", "a", [op("1", "notes", {"id": "n"}), op("2", "go", {"x": "y"}, "event")])
    assert r["notify"] == [{"title": "go", "body": "y"}]
    assert r["pull"]["seen"] == [{"id": "1", "by": "a"}] and r["pull"]["ticks"][0]["n"] == 1
    assert json.loads(h("POST", "/bot/hook", {"x-who": "n8n"}, b'{"a":1}')[2]) == {"who": "n8n", "got": {"a": 1}}
    assert h("POST", "/open/hook", {}, b"{}")[0] == 404 and h("POST", "/bot/hook", {}, b"[1]")[0] == 400
    data = json.loads((root / "bot" / "server-data.json").read_text())  # the shape the TypeScript server writes
    assert set(data) == {"seq", "seen", "devices", "push", "notes", "rows"} and data["rows"]["seen"]["1"]["origin"] == "server"
    assert data["rows"]["notes"]["n"]["origin"] == "a"
    assert data["seen"] == ["1", "2"]


def test_registered_bot_and_state_survives_restart(root):
    class Bot:
        def on_op(self, op, store, device):
            store.put("replies", {"id": "re", "text": "echo"})

    h = create_handler(str(root), bots={"open": Bot()})
    sync(h, "open", "a", [op("1", "notes", {"id": "n"})])
    again = create_handler(str(root))
    assert sync(again, "open", "b")["pull"]["replies"] == [{"id": "re", "text": "echo"}]
    assert sync(again, "open", "a", [op("1", "notes", {"id": "n"})])["done"] == ["1"]  # op ids are remembered too


def test_package_and_listing(root):
    h = create_handler(str(root))
    (root / "open" / "pages" / "chat.oui").write_text("chat")
    meta = json.loads((root / "open" / "space.json").read_text())
    meta.update({"$schema": "x", "pageScroll": {"chat": "bottom"}})
    (root / "open" / "space.json").write_text(json.dumps(meta))
    pkg = json.loads(h("GET", "/open/space", {})[2])
    assert pkg["pages"] == {"chat": {"ui": "chat", "scroll": "bottom"}, "home": "root = Page([])"}
    assert "$schema" not in pkg and "pageScroll" not in pkg
    assert json.loads(h("GET", "/", {})[2]) == ["locked", "open", "shared"]


def test_wsgi_and_asgi(root):
    import asyncio

    app = create_app(str(root))
    out = {}
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/open/space", "QUERY_STRING": "", "SCRIPT_NAME": ""}
    body = b"".join(app(environ, lambda s, h: out.update(status=s, headers=h)))
    assert out["status"] == "200 OK" and json.loads(body)["name"] == "open"

    sent = []

    async def receive():
        return {"type": "http.request", "body": b'{"device":"a"}', "more_body": False}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": "/open/sync", "raw_path": b"/open/sync", "query_string": b"", "headers": [], "root_path": ""}
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 200 and json.loads(sent[1]["body"])["done"] == []
