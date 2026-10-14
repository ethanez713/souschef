"""The account a kitchen belongs to.

Pantry, recipes, plan, shopping list and receipts hang off one user. The
ingredient catalog is shared reference data.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, TimestampMixin

INVITE_CODE_BYTES = 24


class User(Base, TimestampMixin):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Stored casefolded so "Ethan" and "ethan" cannot become two accounts;
    # `display_name` keeps whatever capitalization was typed.
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))

    # Filled when a row is created. Nothing checks the value.
    password_hash: Mapped[str] = mapped_column(String(255))

    is_admin: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)

    last_login_at: Mapped[datetime | None] = mapped_column()

    # Display only; stored quantities are always base units.
    unit_system: Mapped[str] = mapped_column(String(6), default="us", server_default="us")
    show_alt_units: Mapped[bool] = mapped_column(default=False, server_default="0")

    session_epoch: Mapped[int] = mapped_column(default=0)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.username!r}{' admin' if self.is_admin else ''}>"


class Invite(Base, TimestampMixin):
    """Table kept so an existing database still opens."""

    __tablename__ = "invite"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column()

    redeemed_at: Mapped[datetime | None] = mapped_column()
    redeemed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )

    note: Mapped[str | None] = mapped_column(String(120))  # "for Jordan"

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    redeemed_by: Mapped[User | None] = relationship(foreign_keys=[redeemed_by_id])

    def __repr__(self) -> str:
        state = "redeemed" if self.redeemed_at else "open"
        return f"<Invite {self.id} {state}>"
