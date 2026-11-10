import json
import os

import pytest


def make_space(root, name, auth="open", version=1, auth_file=None, bot=None):
    d = root / name
    (d / "pages").mkdir(parents=True)
    (d / "space.json").write_text(json.dumps({"name": name, "version": version, "auth": auth, "sync": {"tables": []}, "schema": []}))
    (d / "pages" / "home.oui").write_text("root = Page([])")
    if auth_file is not None:
        (d / "auth.json").write_text(json.dumps(auth_file))
    if bot:
        (d / "bot.py").write_text(bot)
    return d


@pytest.fixture
def root(tmp_path):
    make_space(tmp_path, "open", "open")
    make_space(tmp_path, "shared", "token", auth_file={"token": "s3cret"})
    make_space(tmp_path, "locked", "device", auth_file={"invites": [{"code": "ABCD-EFGH", "uses": 1}]})
    return tmp_path
