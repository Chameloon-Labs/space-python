"""Who may reach a space (space.json `auth`: "open", "token" or "device"). Same rules and files as the TypeScript SDK.

Secrets live in <space>/auth.json (never served):
  {"token": "...", "invites": [{"code": "ABCD-EFGH", "uses": 1, "note": "..."}],
   "devices": [{"key": "<base64 public key>", "device": "<phone id>", "added": "<ISO time>", "note": "..."}]}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

SKEW_MS = 5 * 60 * 1000  # how far a phone's clock may be off
REPLAY_MS = 2 * SKEW_MS  # a signature is refused again for this long
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def auth_path(directory: str) -> str:
    return os.path.join(directory, "auth.json")


def read_auth(directory: str) -> dict[str, Any]:
    try:
        with open(auth_path(directory), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_auth(directory: str, auth: dict[str, Any]) -> None:
    tmp = auth_path(directory) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2, ensure_ascii=False)
    os.replace(tmp, auth_path(directory))  # never leave a half-written secrets file


def canonical(method: str, target: str, time_ms: str, body: bytes) -> str:
    """What a phone signs: METHOD, path?query, time and the body's SHA-256, one per line."""
    return f"{method.upper()}\n{target}\n{time_ms}\n{hashlib.sha256(body).hexdigest()}"


def _equal(a: str, b: str) -> bool:
    """Constant-time comparison, so the answer time doesn't give away how much of a guess was right."""
    return hmac.compare_digest(a.encode(), b.encode())


class ReplayCache:
    """Signed requests (key + what was signed) used in the last minutes. Thread-safe: `add` is the only way in, and it is atomic."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def __contains__(self, sig: str) -> bool:
        with self._lock:
            return sig in self._seen

    def add(self, sig: str) -> bool:
        """Remembers `sig`; False if it was already there (two parallel replays: only one wins)."""
        now = time.time() * 1000
        with self._lock:
            for s in [s for s, t in self._seen.items() if now - t > REPLAY_MS]:
                del self._seen[s]
            if sig in self._seen:
                return False
            self._seen[sig] = now
            return True


def _verify(key_b64: str, sig_b64: str, data: bytes) -> bool:
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(key_b64, validate=True))
        raw = base64.b64decode(sig_b64, validate=True)
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def check_access(
    directory: str,
    mode: str | None,
    method: str,
    target: str,
    headers: Mapping[str, str],
    body: bytes,
    replays: ReplayCache,
    lock: threading.Lock,
) -> tuple[int, str] | None:
    """Checks a request against the space's `auth`. `target` is the path and query as requested, `lock` guards auth.json."""
    if not mode or mode == "open":
        return None
    if mode == "token":
        token = read_auth(directory).get("token")
        given = headers.get("authorization", "")
        given = given[7:] if given.startswith("Bearer ") else given
        if token and _equal(given, token):
            return None
        return 401, "This space needs its token."
    key = headers.get("x-chameloon-key", "")
    stamp = headers.get("x-chameloon-time", "")
    sig = headers.get("x-chameloon-signature", "")
    if not key or not stamp or not sig:
        return 401, "This space needs a signed request from the Chameloon app."
    try:
        off = abs(time.time() * 1000 - float(stamp))
    except ValueError:
        off = float("nan")
    if not off < SKEW_MS:
        return 401, "The phone's clock is off by more than 5 minutes."
    signed = canonical(method, target, stamp, body)
    if not _verify(key, sig, signed.encode()):
        return 401, "The signature doesn't match."
    # remember what was signed, not the signature: an ECDSA signature has a second valid form (s -> n - s)
    if not replays.add(hashlib.sha256(f"{key}\n{signed}".encode()).hexdigest()):
        return 401, "This request was already used."

    with lock:  # auth.json is read, changed and written back
        auth = read_auth(directory)
        if any(d.get("key") == key for d in auth.get("devices") or []):
            return None
        # a phone the server doesn't know yet: its invite code lets it in, once per code use
        code = headers.get("x-chameloon-invite", "")
        invites = auth.get("invites") or []
        invite = next((i for i in invites if code and _equal(i["code"], code) and (i.get("uses") is None or i["uses"] > 0)), None)
        if invite is None:
            return 401, "This space needs an invite code."
        if invite.get("uses") is not None:
            invite["uses"] -= 1
        auth["invites"] = [i for i in invites if i.get("uses") is None or i["uses"] > 0]
        entry: dict[str, Any] = {"key": key}
        if headers.get("x-chameloon-device"):
            entry["device"] = headers["x-chameloon-device"]
        entry["added"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if invite.get("note"):
            entry["note"] = invite["note"]
        auth["devices"] = [*(auth.get("devices") or []), entry]
        write_auth(directory, auth)
    return None


def new_invite(directory: str, uses: int | None = 1, note: str | None = None) -> str:
    """A new invite code like "K7QM-3XRP". `uses=None` is unlimited."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    auth = read_auth(directory)
    auth["invites"] = [*(auth.get("invites") or []), {"code": code, "uses": uses, **({"note": note} if note else {})}]
    write_auth(directory, auth)
    return code


def revoke(directory: str, prefix: str) -> int:
    """Removes the phones whose key or phone id starts with `prefix`; returns how many."""
    auth = read_auth(directory)
    before = auth.get("devices") or []
    auth["devices"] = [d for d in before if not (d["key"].startswith(prefix) or (d.get("device") or "").startswith(prefix))]
    write_auth(directory, auth)
    return len(before) - len(auth["devices"])
