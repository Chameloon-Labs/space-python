import os

from chameloon_space import read_auth
from chameloon_space.cli import devices_lines, main


def test_invite_devices_revoke(root, capsys):
    folder = str(root)
    assert main(["invite", folder, "locked", "--uses", "unlimited", "--note", "Anna"]) == 0
    code = capsys.readouterr().out.strip()
    assert read_auth(str(root / "locked"))["invites"][-1] == {"code": code, "uses": None, "note": "Anna"}
    assert main(["invite", folder, "locked", "--uses", "3"]) == 0
    assert read_auth(str(root / "locked"))["invites"][-1]["uses"] == 3
    capsys.readouterr()

    auth = read_auth(str(root / "locked"))
    auth["devices"] = [{"key": "A" * 40, "device": "phone-1", "added": "2026-01-01T00:00:00.000Z", "note": "Anna"}, {"key": "B" * 40, "added": "2026-01-02T00:00:00.000Z"}]
    (root / "locked" / "auth.json").write_text(__import__("json").dumps(auth))
    assert devices_lines(str(root / "locked")) == [
        "AAAAAAAAAAAAAAAA…  phone phone-1  since 2026-01-01T00:00:00.000Z  (Anna)",
        "BBBBBBBBBBBBBBBB…  phone ?  since 2026-01-02T00:00:00.000Z",
    ]
    assert main(["revoke", folder, "locked", "phone-1"]) == 0
    assert capsys.readouterr().out == "revoked 1 phone(s)\n"


def test_unknown_space(root, capsys):
    assert main(["devices", str(root), "nope"]) == 1
    assert 'No space "nope"' in capsys.readouterr().err
