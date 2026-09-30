"""Authentication: accounts, sessions, and invites.

The app runs on your own machine, but it holds each user's NYT session cookie
and can be reached from the rest of the house, so this is not decorative. It is
still deliberately small: a username, a password, a signed session cookie, and an
invite to create the next account. No password reset by email, no roles beyond
"can issue invites" -- each of those would be attack surface protecting nothing
on a kitchen app used by a handful of people.

Account creation is closed. The first request to an empty database claims the
admin account; after that a new account needs a single-use invite. That means
there is exactly one anonymous form on the whole site (login) plus a redemption
page that is useless without a 24-byte secret.

Rate limiting is in-process and per-IP, which is the right size for one
machine; a distributed limiter would be ceremony.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.db import get_db
from souschef.logconfig import get_logger
from souschef.models import Invite, User
from souschef.models.user import INVITE_CODE_BYTES

log = get_logger("souschef.auth")

SESSION_COOKIE = "souschef_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days; this is a kitchen app, not a bank

# The plaintext invite is shown once. A query parameter would land in the
# browser history and in any access log that records the request target.
FLASH_COOKIE = "souschef_invite_flash"
FLASH_MAX_AGE = 120

# Per (ip, username): stops someone grinding one account.
MAX_ATTEMPTS = 8
# Per ip across all usernames: stops the same attacker spraying one password
# over many names, which the per-account counter alone would never notice.
MAX_ATTEMPTS_PER_IP = 30
LOCKOUT_SECONDS = 300

MIN_PASSWORD_LENGTH = 12
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")

INVITE_TTL = timedelta(days=7)

_hasher = PasswordHasher()

# A valid-looking argon2 hash of nothing in particular. Verifying against this
# when the username is unknown keeps the failure path the same length as a wrong
# password, so response time doesn't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def normalize_username(raw: str) -> str:
    return raw.strip().casefold()


def validate_username(raw: str) -> str:
    """Return the normalized username, or raise with a usable message."""
    name = normalize_username(raw)
    if not USERNAME_PATTERN.match(name):
        raise ValueError(
            "Username must be 3-32 characters: letters, digits, dot, dash or "
            "underscore, starting with a letter or digit."
        )
    return name


def user_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)) or 0


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == normalize_username(username)))


def create_user(
    db: Session,
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    display_name: str | None = None,
) -> User:
    """Create an account. Raises ValueError on anything the user can fix."""
    name = validate_username(username)
    if get_user_by_username(db, name) is not None:
        raise ValueError("That username is already taken.")

    user = User(
        username=name,
        display_name=(display_name or username).strip()[:64] or name,
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    db.flush()
    log.info("Created account %r (admin=%s)", user.username, is_admin)
    return user


def set_password(db: Session, user: User, password: str) -> None:
    """Change a password and invalidate every existing session for that user."""
    user.password_hash = hash_password(password)
    # Without this a stolen cookie outlives the password change meant to kill it.
    user.session_epoch += 1
    db.flush()
    log.info("Password changed for %r; existing sessions invalidated", user.username)


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    secret = settings.session_secret
    if not secret:
        raise RuntimeError(
            "SOUSCHEF_SESSION_SECRET is not set. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    return URLSafeTimedSerializer(secret, salt="souschef-session")


def issue_session(user: User) -> str:
    """Mint a session cookie value for a user.

    Carries the account's ``session_epoch`` so a password change can revoke
    every outstanding cookie without keeping server-side session state.
    """
    return _serializer().dumps(
        {"v": 2, "uid": user.id, "epoch": user.session_epoch, "nonce": secrets.token_urlsafe(8)}
    )


def read_session(token: str | None) -> tuple[int, int] | None:
    """Return ``(user_id, epoch)`` from a cookie, or None if it isn't valid."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(data, dict) or data.get("v") != 2:
        # v1 cookies belong to the single-password era and identify no user.
        return None
    uid, epoch = data.get("uid"), data.get("epoch")
    if not isinstance(uid, int) or not isinstance(epoch, int):
        return None
    return uid, epoch


class _Redirect(HTTPException):
    """A 303 to the login page, used where an API would return 401."""

    def __init__(self, location: str = "/login") -> None:
        super().__init__(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": location},
            detail="Not authenticated",
        )


def login_location(request: Request) -> str:
    """Sign-in URL that remembers the page the person was trying to open.

    The raw query is already percent-encoded. Quoting it again would turn the
    shared recipe URL into a second encoding, and the form would not show the
    address the person shared.
    """
    path = request.url.path
    if request.url.query:
        path = f"{path}?{unquote(request.url.query)}"
    return "/login?next=" + quote(path, safe="")


def issue_invite_flash(code: str) -> str:
    return _serializer().dumps({"code": code})


def read_invite_flash(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=FLASH_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    return code if isinstance(code, str) and code.strip() else None


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency resolving the signed-in user.

    A valid signature is not enough on its own. The account must still exist, be
    active, and match the session epoch -- otherwise a cookie issued before a
    deletion, suspension or password change would keep working.
    """
    parsed = read_session(request.cookies.get(SESSION_COOKIE))
    if parsed is None:
        raise _Redirect(login_location(request))

    user_id, epoch = parsed
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.session_epoch != epoch:
        raise _Redirect(login_location(request))

    # Stashed so template rendering can pick it up without every route having to
    # pass it through explicitly -- the nav needs it on every authenticated page.
    request.state.user = user
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


@dataclass
class _Attempts:
    count: int = 0
    first_at: float = field(default_factory=time.monotonic)


_failures: dict[str, _Attempts] = {}


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check(key: str, cap: int) -> bool:
    record = _failures.get(key)
    if record is None:
        return False
    if time.monotonic() - record.first_at > LOCKOUT_SECONDS:
        del _failures[key]
        return False
    return record.count >= cap


def is_locked_out(ip: str, username: str) -> bool:
    return _check(f"ip:{ip}", MAX_ATTEMPTS_PER_IP) or _check(
        f"acct:{ip}|{normalize_username(username)}", MAX_ATTEMPTS
    )


def _bump(key: str) -> int:
    record = _failures.get(key)
    if record is None or time.monotonic() - record.first_at > LOCKOUT_SECONDS:
        _failures[key] = _Attempts(count=1)
        return 1
    record.count += 1
    return record.count


def record_failure(ip: str, username: str) -> None:
    name = normalize_username(username)
    per_ip = _bump(f"ip:{ip}")
    per_account = _bump(f"acct:{ip}|{name}")

    if per_account >= MAX_ATTEMPTS or per_ip >= MAX_ATTEMPTS_PER_IP:
        # The one sign-in log line worth acting on: someone is guessing.
        log.error(
            "Locked out %s after %d failed sign-ins for %r (%d from this address); "
            "blocked for %ds",
            ip, per_account, name, per_ip, LOCKOUT_SECONDS,
        )
    else:
        log.warning(
            "Failed sign-in from %s for %r (%d/%d)", ip, name, per_account, MAX_ATTEMPTS
        )


def clear_failures(ip: str, username: str) -> None:
    _failures.pop(f"ip:{ip}", None)
    _failures.pop(f"acct:{ip}|{normalize_username(username)}", None)


def authenticate(db: Session, username: str, password: str) -> User | None:
    """Check credentials, in constant-ish time whether or not the user exists."""
    user = get_user_by_username(db, username)
    if user is None:
        # Burn the same argon2 work as a real check so timing doesn't leak
        # which usernames are registered.
        verify_password(_DUMMY_HASH, password)
        return None
    if not user.is_active:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password):
        return None

    user.last_login_at = datetime.now(UTC)
    db.flush()
    return user


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hash_invite_code(code: str) -> str:
    """Hash an invite code for storage.

    SHA-256 rather than argon2 on purpose: the code is 24 random bytes, so there
    is no dictionary to run against the hash and a slow KDF would buy nothing.
    Storing the hash still means a leaked database yields no usable invites.
    """
    return hashlib.sha256(code.strip().encode()).hexdigest()


def create_invite(db: Session, creator: User, *, note: str | None = None) -> tuple[Invite, str]:
    """Mint a single-use invite. Returns the row and the *plaintext* code.

    The plaintext is shown once, at creation, and never stored -- so a lost code
    is reissued rather than recovered.
    """
    code = secrets.token_urlsafe(INVITE_CODE_BYTES)
    invite = Invite(
        code_hash=hash_invite_code(code),
        created_by_id=creator.id,
        expires_at=datetime.now(UTC) + INVITE_TTL,
        note=(note or "").strip()[:120] or None,
    )
    db.add(invite)
    db.flush()
    log.info("Invite %d created by %r", invite.id, creator.username)
    return invite, code


def find_usable_invite(db: Session, code: str) -> Invite | None:
    """Look up an unredeemed, unexpired invite by its plaintext code."""
    if not code or not code.strip():
        return None
    invite = db.scalar(select(Invite).where(Invite.code_hash == hash_invite_code(code)))
    if invite is None or invite.redeemed_at is not None:
        return None

    expires = invite.expires_at
    if expires.tzinfo is None:
        # SQLite hands back naive datetimes; compare in UTC rather than crashing.
        expires = expires.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        return None
    return invite


def redeem_invite(
    db: Session, code: str, username: str, password: str, display_name: str | None = None
) -> User:
    """Create an account against an invite, consuming it. Raises ValueError."""
    invite = find_usable_invite(db, code)
    if invite is None:
        raise ValueError("That invite is invalid, already used, or expired.")

    user = create_user(db, username, password, display_name=display_name)

    invite.redeemed_at = datetime.now(UTC)
    invite.redeemed_by_id = user.id
    db.flush()
    log.info("Invite %d redeemed by %r", invite.id, user.username)
    return user
