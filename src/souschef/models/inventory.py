"""Pantry state and the append-only event ledger behind it."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, TimestampMixin, utcnow

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
    WASTE = "waste"  # thrown out
    CONFIRM = "confirm"  # user physically checked; level unchanged, confidence restored
    INITIAL = "initial"  # seeding an item for the first time


class PantryItem(Base, TimestampMixin):
    """Current believed state of one ingredient in the kitchen.

    This is a *materialized view* of the event ledger, not the source of truth.
    It exists so the common read path is a single indexed row rather than a fold
    over every event. Rebuild it from ``InventoryEvent`` at any time.
    """

    __tablename__ = "pantry_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="CASCADE"), unique=True, index=True
    )

    # Quantity in the ingredient's base_unit (g / ml / count). Authoritative for
    # discrete items; a best estimate for bulk ones.
    quantity: Mapped[float] = mapped_column(Float, default=0.0)

    # How much we trust `quantity` right now, in [0, 1]. Decays with elapsed time
    # and with every cook event that debited an estimate rather than a known
    # amount. Restored to 1.0 by a physical confirmation.
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    # When the user last physically looked at this item. The anchor that decay
    # is measured from -- not `updated_at`, which a cook event also touches.
    last_confirmed_at: Mapped[datetime] = mapped_column(default=utcnow)

    # Cook events applied since the last physical confirmation. Each one
    # compounds estimation error, so this drives an extra confidence penalty.
    cooks_since_confirmed: Mapped[int] = mapped_column(default=0)

    location: Mapped[StorageLocation] = mapped_column(
        Enum(StorageLocation, native_enum=False, length=10), default=StorageLocation.PANTRY
    )
    expires_on: Mapped[date | None] = mapped_column(Date)

    # User-set restock floor in base units. When the level drops below this the
    # item becomes a restock candidate even if no planned recipe needs it.
    restock_threshold: Mapped[float | None] = mapped_column(Float)

    ingredient: Mapped[CanonicalIngredient] = relationship(back_populates="pantry_item")

    def __repr__(self) -> str:
        return (
            f"<PantryItem ing={self.ingredient_id} "
            f"qty={self.quantity:.1f} c={self.confidence:.2f}>"
        )


class InventoryEvent(Base):
    """Append-only ledger of every change to pantry state.

    Nothing mutates a level except by writing one of these first. That buys
    three things cheaply: state is always reconstructible, "why does it think I
    have three onions?" is answerable, and undo is just ignoring an event.
    """

    __tablename__ = "inventory_event"
    __table_args__ = (Index("ix_event_ingredient_time", "ingredient_id", "occurred_at"),)

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
    resulting_quantity: Mapped[float] = mapped_column(Float, default=0.0)

    # How sure we are of *this* event. A receipt line matched at 0.6 produces a
    # less trustworthy purchase than a manually keyed one.
    event_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    occurred_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)

    # Provenance: "receipt:12", "recipe:44", "user". Enough to trace any number
    # on screen back to the thing that caused it.
    source_ref: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(300))

    def __repr__(self) -> str:
        return f"<InventoryEvent {self.reason} ing={self.ingredient_id} d={self.delta:+.1f}>"
