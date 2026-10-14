"""Persisted app settings and encrypted third-party credentials."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Enum, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from souschef.models.base import Base, OwnedMixin, TimestampMixin


class AppSetting(Base, TimestampMixin):
    """Small key/value store for *app-wide* settings. Never holds secrets.

    Not per-user: anything belonging to one account goes on ``User``.
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class CredentialKind(enum.StrEnum):
    NYT_COOKING_COOKIE = "nyt_cooking_cookie"


class StoredCredential(Base, OwnedMixin, TimestampMixin):
    """A third-party credential, encrypted at rest.

    The ciphertext lives here; the key does not -- it comes from the host secret
    store at runtime. So a stolen database file, or a leaked backup, yields
    nothing usable on its own.

    Only the NYT *session cookie* is ever stored, never the account password.
    NYT gates login behind bot detection, so automating it is both fragile and a
    good way to get an account locked; a session cookie is also far less
    valuable to an attacker than full account credentials.

    Per user, emphatically: this is someone's paid NYT subscription, and sharing
    one cookie across accounts would both leak a credential and get it revoked
    for concurrent use.
    """

    __tablename__ = "stored_credential"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_credential_user_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[CredentialKind] = mapped_column(
        Enum(CredentialKind, native_enum=False, length=32)
    )

    # AES-GCM: nonce prepended to ciphertext.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)

    # Non-sensitive metadata so the UI can show status without decrypting.
    label: Mapped[str | None] = mapped_column(String(120))
    last_used_at: Mapped[datetime | None] = mapped_column()
    last_success_at: Mapped[datetime | None] = mapped_column()
    is_working: Mapped[bool] = mapped_column(default=True)

    def __repr__(self) -> str:
        return f"<StoredCredential {self.kind} working={self.is_working}>"
