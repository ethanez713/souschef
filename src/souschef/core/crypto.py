"""Encryption for stored third-party credentials.

AES-256-GCM with a key that lives in the host secret store, never in the
database and never in the repo. The threat this actually addresses is a leaked
database file or backup -- the most likely way a single-user app spills a
secret. It does not defend against an attacker who already has code execution
on the box, and nothing at this scale would.

Only the NYT *session cookie* is ever stored. The account password is never
accepted, because NYT gates login behind bot detection (making automation both
fragile and a route to a locked account) and because a session cookie is a far
smaller prize than full account credentials.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from souschef.config import get_settings

NONCE_BYTES = 12
KEY_BYTES = 32


class CredentialError(RuntimeError):
    """Encryption key missing, or ciphertext failed authentication."""


def _key() -> bytes:
    """Load and validate the encryption key.

    Accepts 64 hex chars or raw bytes. A short or missing key fails loudly:
    silently falling back to a default would produce ciphertext that looks
    encrypted and protects nothing.
    """
    settings = get_settings()
    raw = settings.secret_key
    if not raw:
        raise CredentialError(
            "No encryption key configured. Set SOUSCHEF_SECRET_KEY (64 hex chars) "
            "or SOUSCHEF_SECRET_KEY_FILE. Generate one with: "
            "python -c 'import secrets; print(secrets.token_hex(32))'"
        )

    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = raw.encode("utf-8")

    if len(key) != KEY_BYTES:
        raise CredentialError(
            f"Encryption key must be {KEY_BYTES} bytes ({KEY_BYTES * 2} hex chars); "
            f"got {len(key)}."
        )
    return key


def encrypt(plaintext: str) -> bytes:
    """Encrypt a secret. Returns nonce || ciphertext."""
    if not plaintext:
        raise CredentialError("Refusing to encrypt an empty value")
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)


def decrypt(blob: bytes) -> str:
    """Decrypt a stored secret.

    A wrong key or tampered ciphertext raises rather than returning garbage --
    GCM authenticates, so this is a real integrity check, not just a decode.
    """
    if not blob or len(blob) <= NONCE_BYTES:
        raise CredentialError("Stored credential is empty or truncated")
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(_key()).decrypt(nonce, ciphertext, None).decode("utf-8")
    except InvalidTag as exc:
        raise CredentialError(
            "Could not decrypt stored credential: wrong key, or the value was tampered with. "
            "If the key was rotated, re-enter the credential."
        ) from exc


def generate_key() -> str:
    """A fresh key, hex-encoded. Used by the setup command."""
    import secrets

    return secrets.token_hex(KEY_BYTES)
