"""Single-user authentication.

The app is internet-facing and holds a NYT session cookie, so this is not
decorative. It is still deliberately small: one user, a password, a signed
session cookie. No registration, no password reset, no roles -- each of those
would be extra attack surface protecting nothing.

Rate limiting is in-process and per-IP. That is the right size for a single-user
app on one box; a distributed limiter would be ceremony.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from souschef.config import get_settings
from souschef.logconfig import get_logger

log = get_logger("souschef.auth")

SESSION_COOKIE = "souschef_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days; this is a kitchen app, not a bank

MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300

_hasher = PasswordHasher()


@dataclass
class _Attempts:
    count: int = 0
    first_at: float = field(default_factory=time.monotonic)


_failures: dict[str, _Attempts] = {}


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    secret = settings.session_secret
    if not secret:
        raise RuntimeError(
            "SOUSCHEF_SESSION_SECRET is not set. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    return URLSafeTimedSerializer(secret, salt="souschef-session")


def issue_session() -> str:
    return _serializer().dumps({"v": 1, "nonce": secrets.token_urlsafe(8)})


def validate_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return False
    return True


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_locked_out(key: str) -> bool:
    record = _failures.get(key)
    if record is None:
        return False
    if time.monotonic() - record.first_at > LOCKOUT_SECONDS:
        del _failures[key]
        return False
    return record.count >= MAX_ATTEMPTS


def record_failure(key: str) -> None:
    record = _failures.get(key)
    if record is None or time.monotonic() - record.first_at > LOCKOUT_SECONDS:
        _failures[key] = _Attempts(count=1)
        log.warning("Failed sign-in from %s (1/%d)", key, MAX_ATTEMPTS)
        return

    record.count += 1
    if record.count >= MAX_ATTEMPTS:
        # On a public box this is the signal that matters: someone is guessing.
        log.error(
            "Locked out %s after %d failed sign-ins; blocked for %ds",
            key, record.count, LOCKOUT_SECONDS,
        )
    else:
        log.warning("Failed sign-in from %s (%d/%d)", key, record.count, MAX_ATTEMPTS)


def clear_failures(key: str) -> None:
    _failures.pop(key, None)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def require_session(request: Request) -> None:
    """FastAPI dependency guarding every non-public route."""
    if not validate_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
            detail="Not authenticated",
        )
