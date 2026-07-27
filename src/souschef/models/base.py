"""Declarative base and shared column types."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Explicit naming convention so Alembic can autogenerate reversible constraint
# migrations -- SQLite in particular needs named constraints to alter tables.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {datetime: DateTime(timezone=True)}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class OwnedMixin:
    """Marks a table as belonging to exactly one user.

    Every row a user creates -- pantry levels, recipes, plans, receipts -- hangs
    off this column, and every query that reads them must filter on it. The
    column is non-nullable on purpose: a nullable owner invites a query that
    forgets the filter and quietly returns another user's kitchen.

    Cascade on delete, because a deleted account should take its data with it
    rather than leave orphans pointing at a missing user.
    """

    @declared_attr
    @classmethod
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
