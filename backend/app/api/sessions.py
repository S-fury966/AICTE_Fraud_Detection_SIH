"""Sign-in for the roles that may not simply be asserted.

An officer names themselves in a header. A senior reviewer or administrator
must present the role's passcode once (``POST /api/session``) and then carries
a signed token: ``actor|role|expiry`` with an HMAC over it. The role in a token
is the only way to hold one of these roles, so a header claiming
``senior_reviewer`` is refused rather than believed.

Passcodes are PBKDF2-SHA256 hashes in ``config/api.yaml``. The signing key is
``SIH_SESSION_SECRET`` when set, otherwise random per process: restarting the
API then signs everyone out, which is the safe default for a prototype.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..core.config import get


def hash_passcode(passcode: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, iterations).hex()


@dataclass
class Session:
    actor: str
    role: str
    expires_at: int


class SessionError(Exception):
    """A token that is malformed, tampered with or expired."""


class Sessions:
    def __init__(self, config: dict[str, Any]) -> None:
        self.passcodes: dict[str, dict[str, Any]] = dict(get(config, "sign_in.passcodes", {}) or {})
        self.token_seconds = int(get(config, "sign_in.token_minutes", 480)) * 60
        self.max_failures = int(get(config, "sign_in.max_failures", 5))
        self.window = int(get(config, "sign_in.failure_window_seconds", 300))
        secret = os.environ.get("SIH_SESSION_SECRET", "")
        self._key = secret.encode("utf-8") if secret else secrets.token_bytes(32)
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def requires_sign_in(self, role: str) -> bool:
        return role in self.passcodes

    # ------------------------------------------------------------------
    # Passcodes, with a per-role limit on refusals
    # ------------------------------------------------------------------

    def locked_out(self, role: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(role, []) if now - t < self.window]
            self._failures[role] = recent
            return len(recent) >= self.max_failures

    def check_passcode(self, role: str, passcode: str) -> bool:
        entry = self.passcodes.get(role)
        if not entry:
            return False
        expected = str(entry["pbkdf2_sha256"])
        actual = hash_passcode(passcode, bytes.fromhex(str(entry["salt"])), int(entry["iterations"]))
        ok = hmac.compare_digest(actual, expected)
        if not ok:
            with self._lock:
                self._failures.setdefault(role, []).append(time.time())
        return ok

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    def issue(self, actor: str, role: str) -> Session:
        return Session(actor=actor, role=role, expires_at=int(time.time()) + self.token_seconds)

    def encode(self, session: Session) -> str:
        body = f"{session.actor}|{session.role}|{session.expires_at}".encode("utf-8")
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return f"{_b64(body)}.{_b64(signature)}"

    def decode(self, token: str) -> Session:
        try:
            body_part, signature_part = token.split(".", 1)
            body = _unb64(body_part)
            signature = _unb64(signature_part)
        except ValueError as exc:
            raise SessionError("malformed sign-in token") from exc
        if not hmac.compare_digest(signature, hmac.new(self._key, body, hashlib.sha256).digest()):
            raise SessionError("sign-in token is not valid; sign in again")
        actor, role, expires = body.decode("utf-8").rsplit("|", 2)
        if int(expires) < time.time():
            raise SessionError("sign-in has expired; sign in again")
        return Session(actor=actor, role=role, expires_at=int(expires))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("not base64") from exc
