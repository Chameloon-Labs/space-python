"""chameloon-serve-py <folder> [--port 8787] [--token secret]  and the invite / devices / revoke helpers.

Same commands and output as the TypeScript `chameloon-serve`; `python -m chameloon_space serve <folder>` works too.
"""
from __future__ import annotations

import os
import sys

from .app import serve
from .auth import new_invite, read_auth, revoke

USAGE = """Usage:
  chameloon-serve-py [serve] <spaces folder> [--port 8787] [--token secret]
  chameloon-serve-py invite  <spaces folder> <space> [--uses N|unlimited] [--note "Anna's phone"]
  chameloon-serve-py devices <spaces folder> <space>
  chameloon-serve-py revoke  <spaces folder> <space> <key or phone id prefix>"""


def _flag(args: list[str], name: str) -> str | None:
    """Takes `--name value` out of args wherever it is."""
    if f"--{name}" not in args:
        return None
    i = args.index(f"--{name}")
    value = args[i + 1] if i + 1 < len(args) else None
    del args[i : i + 2]
    return value


def devices_lines(directory: str) -> list[str]:
    return [
        f"{d['key'][:16]}…  phone {d.get('device') or '?'}  since {d['added']}{'  (' + d['note'] + ')' if d.get('note') else ''}"
        for d in read_auth(directory).get("devices") or []
    ]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    command = args.pop(0) if args and args[0] in ("serve", "invite", "devices", "revoke") else "serve"
    port = _flag(args, "port")
    token = _flag(args, "token") or os.environ.get("TOKEN") or None
    uses, note = _flag(args, "uses"), _flag(args, "note")
    root = os.path.abspath(args[0] if args else os.environ.get("SPACES", "spaces"))

    if command != "serve":
        name = args[1] if len(args) > 1 else ""
        directory = os.path.join(root, name)
        if not name or not os.path.exists(os.path.join(directory, "space.json")):
            print(f'No space "{name}" in {root}.', file=sys.stderr)
            return 1
        if command == "invite":
            try:
                count = None if uses == "unlimited" else int(uses if uses is not None else 1)
            except ValueError:
                print(f'--uses needs a number or "unlimited", not "{uses}".', file=sys.stderr)
                return 1
            print(new_invite(directory, count, note))
        elif command == "devices":
            for line in devices_lines(directory):
                print(line)
        else:
            print(f"revoked {revoke(directory, args[2] if len(args) > 2 else chr(0))} phone(s)")
        return 0

    if not os.path.exists(root):
        print(f"No folder {root}. {USAGE}", file=sys.stderr)
        return 1
    try:
        serve(root, int(port or os.environ.get("PORT") or 8787), token)
    except ValueError:
        print(f'--port needs a number, not "{port}".', file=sys.stderr)
        return 1
    return 0
