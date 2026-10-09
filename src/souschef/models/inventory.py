"""Pantry state and the append-only event ledger behind it."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, OwnedMixin, TimestampMixin, utcnow

if TYPE_CHECKING:
    from souschef.models.ingredient import CanonicalIngredient


class StorageLocation(enum.StrEnum):
    PANTRY = "pantry"
    FRIDGE = "fridge"
    FREEZER = "freezer"
    OTHER = "other"


class EventReason(enum.StrEnum):
    PURCHASE = "purchase"  # from a receipt or manual add
    COOK = "cook"  # debited by cooking a recipe
    MANUAL = "manual"  # user corrected the level directly
    WASTE = "waste"
    CONFIRM = "confirm"  # user physically checked; level unchanged, range collapsed
    INITIAL = "initial"  # seeding an item for the first time


class PantryItem(Base, OwnedMixin, TimestampMixin):
    """Current believed state of one ingredient in one user's kitchen.

    This is a *materialized view* of the event ledger, not the source of truth.
    It exists so the common read path is a single indexed row rather than a fold
    over every event. Rebuild it from ``InventoryEvent`` at any time.
    """

    __tablename__ = "pantry_item"
    __table_args__ = (
        # One row per ingredient *per user*. Ingredient alone was unique when
        # there was only one kitchen; keeping it that way would let the first
        # user to stock an ingredient block everyone else from tracking it.
        UniqueConstraint("user_id", "ingredient_id", name="uq_pantry_item_user_ingredient"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="CASCADE"), index=True
    )

    # Quantity in the ingredient's base_unit (g / ml / count). Authoritative for
    # discrete items; a best estimate for bulk ones.
    quantity: Mapped[float] = mapped_column(Float, default=0.0)

    # The range the level could plausibly sit in, in base units, with
    # 0 <= low <= quantity <= high. `low` decays with time since `belief_at`
    # (unlogged use only lowers a level); reads apply that drift.
    low: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)

    # The `occurred_at` of the last event folded into this row.
    belief_at: Mapped[datetime | None] = mapped_column()

    # When the user last physically looked at this item. Spoilage measures
    # staleness from it; `updated_at` would be touched by a cook as well.
    last_confirmed_at: Mapped[datetime] = mapped_column(default=utcnow)

    location: Mapped[StorageLocation] = mapped_column(
        Enum(StorageLocation, native_enum=False, length=10), default=StorageLocation.PANTRY
    )
    expires_on: Mapped[date | None] = mapped_column(Date)

    # User-set restock floor in base units. When the level drops below this the
    # item becomes a restock candidate even if no planned recipe needs it.
    restock_threshold: Mapped[float | None] = mapped_column(Float)

    ingredient: Mapped[CanonicalIngredient] = relationship()

    def __repr__(self) -> str:
        return (
            f"<PantryItem u={self.user_id} ing={self.ingredient_id} "
            f"qty={self.quantity:.1f} [{self.low:.1f}, {self.high:.1f}]>"
        )


class InventoryEvent(Base, OwnedMixin):
    """Append-only ledger of every change to pantry state.

    Nothing mutates a level except by writing one of these first. That buys
    three things cheaply: state is always reconstructible, "why does it think I
    have three onions?" is answerable, and undo is just ignoring an event.
    """

    __tablename__ = "inventory_event"
    __table_args__ = (
        # The replay in `rebuild_from_ledger` reads one user's whole history in
        # order, so the owner leads the index.
        Index("ix_event_user_ingredient_time", "user_id", "ingredient_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[EventReason] = mapped_column(
        Enum(EventReason, native_enum=False, length=10), index=True
    )

    # Signed change in base units: positive for purchases, negative for cooking
    # and waste. Zero for CONFIRM events, which assert confidence, not quantity.
    delta: Mapped[float] = mapped_column(Float, default=0.0)

    # Level after this event, so the ledger can be read without replaying it.
    # For an observation (CONFIRM, MANUAL, INITIAL) it is the level the user saw.
    resulting_quantity: Mapped[float] = mapped_column(Float, default=0.0)

    # Half-width of the doubt around an observed level, or extra doubt on a purchase.
    uncertainty: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    # Undo voids an event instead of writing a compensating one; the fold skips it.
    voided_at: Mapped[datetime | None] = mapped_column()

    # The cook this event belongs to, so undoing the cook voids exactly its events.
    cook_id: Mapped[int | None] = mapped_column(
        ForeignKey("cook.id", ondelete="SET NULL"), index=True
    )

    # How sure we are of *this* event. A receipt line matched at 0.6 produces a
    # less trustworthy purchase than a manually keyed one.
    event_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    occurred_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)

    # Provenance: "receipt:12", "recipe:44", "user". Enough to trace any number
    # on screen back to the thing that caused it.
    source_ref: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(300))

    def __repr__(self) -> str:
        return (
            f"<InventoryEvent u={self.user_id} {self.reason} "
            f"ing={self.ingredient_id} d={self.delta:+.1f}>"
        )
