"""Serves a folder with the echo bot registered, for the language-neutral conformance suite:

    cd conformance && bun run.ts -- python /path/to/sdk-python/tests/conformance_server.py {dir} {port}
"""
import sys

from chameloon_space import serve


class EchoBot:
    def on_op(self, op, store, device):
        if op["op"] == "upsert" and op["table"] == "notes":
            row = op["row"]
            store.put("replies", {"id": "re-" + str(row["id"]), "text": "echo: " + str(row.get("text")), "device": device})

    def on_event(self, event, data, store, device):
        if event == "ping":
            store.put("pongs", {"id": data["id"], "device": device})
        elif event == "notify":
            store.notify(data["title"], data["body"])

    def hook(self, body, store, headers):
        store.put("inbox", {"id": body["id"], "text": body["text"]})
        return {"ok": True, "echoed": body["text"]}


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]), bots={"echo": EchoBot()}, host="127.0.0.1")
