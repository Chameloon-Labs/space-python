import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from chameloon_space import canonical, create_handler, new_invite, read_auth, revoke


class Phone:
    """Signs like the app: X9.63 public key, raw r||s signature."""

    def __init__(self, device="phone-1"):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.key = base64.b64encode(self.private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)).decode()
        self.device = device

    def request(self, method, target, body=b"", invite=None, at=None, sig=None):
        if at is None:  # a phone never signs the same text twice: the same request in the same ms is a replay
            at = self.last = max(int(time.time() * 1000), getattr(self, "last", 0) + 1)
        stamp = str(int(at))
        der = self.private.sign(canonical(method, target, stamp, body).encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        headers = {
            "X-Chameloon-Key": self.key, "X-Chameloon-Time": stamp, "X-Chameloon-Device": self.device,
            "X-Chameloon-Signature": sig or base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode(),
        }
        if invite:
            headers["X-Chameloon-Invite"] = invite
        return method, target, headers, body


def status(handler, request):
    return handler(*request)[0]


def test_open_and_token(root):
    h = create_handler(str(root))
    assert h("GET", "/open/space", {})[0] == 200
    assert h("GET", "/shared/space", {})[0] == 401
    assert h("GET", "/shared/space", {"authorization": "Bearer nope"})[0] == 401
    assert h("GET", "/shared/space", {"Authorization": "Bearer s3cret"})[0] == 200


def test_global_token(root):
    h = create_handler(str(root), token="t")
    assert h("GET", "/open/space", {})[0] == 401
    assert h("GET", "/open/space", {"Authorization": "Bearer t"})[0] == 200


def test_device_invite_once_then_key_alone(root):
    h = create_handler(str(root))
    p = Phone()
    code, _, body = h(*p.request("GET", "/locked/space"))
    assert code == 401 and "invite" in json.loads(body)["error"]
    assert status(h, p.request("GET", "/locked/space", invite="ABCD-EFGH")) == 200
    assert status(h, p.request("GET", "/locked/space")) == 200
    assert status(h, Phone().request("GET", "/locked/space", invite="ABCD-EFGH")) == 401  # used up
    auth = read_auth(str(root / "locked"))
    assert auth["invites"] == [] and auth["devices"][0]["key"] == p.key and auth["devices"][0]["device"] == "phone-1"


def test_replay_forgery_tamper_clock(root):
    h = create_handler(str(root))
    p = Phone()
    assert status(h, p.request("GET", "/locked/space", invite="ABCD-EFGH")) == 200
    once = p.request("GET", "/locked/space")
    assert status(h, once) == 200
    code, _, body = h(*once)
    assert code == 401 and "already used" in json.loads(body)["error"]
    assert status(h, p.request("GET", "/locked/space", sig=base64.b64encode(bytes(64)).decode())) == 401
    assert status(h, p.request("GET", "/locked/space", sig="not base64!")) == 401
    m, t, hd, _ = p.request("POST", "/locked/sync", b'{"device":"a"}')
    assert h(m, t, hd, b'{"device":"b"}')[0] == 401  # body changed after signing
    m, t, hd, b = p.request("GET", "/locked/space?x=1")
    assert h(m, "/locked/space?x=2", hd, b)[0] == 401  # query is signed too
    assert status(h, p.request("GET", "/locked/space", at=time.time() * 1000 - 600_000)) == 401
    assert status(h, p.request("GET", "/locked/space", at=time.time() * 1000 + 600_000)) == 401
    assert status(h, p.request("GET", "/locked/space", at=time.time() * 1000 - 120_000)) == 200


def test_unlimited_invite_and_revoke(root):
    d = str(root / "locked")
    code = new_invite(d, None, "Anna's phone")
    assert len(code) == 9 and code[4] == "-"
    h = create_handler(str(root))
    a, b = Phone("anna-phone"), Phone("bob-phone")
    assert status(h, a.request("GET", "/locked/space", invite=code)) == 200
    assert status(h, b.request("GET", "/locked/space", invite=code)) == 200
    assert read_auth(d)["devices"][0]["note"] == "Anna's phone"
    assert revoke(d, "anna") == 1
    assert status(h, a.request("GET", "/locked/space")) == 401
    assert status(h, b.request("GET", "/locked/space")) == 200
    assert revoke(d, "\0") == 0
