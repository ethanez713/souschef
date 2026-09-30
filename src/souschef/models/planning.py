"""Meal plans and shopping lists."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, OwnedMixin, TimestampMixin

if TYPE_CHECKING:
    from souschef.models.ingredient import CanonicalIngredient
    from souschef.models.recipe import Recipe


class MealSlot(enum.StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    OTHER = "other"


class MealPlanEntry(Base, OwnedMixin, TimestampMixin):
    __tablename__ = "meal_plan_entry"
    __table_args__ = (Index("ix_mealplan_user_date_slot", "user_id", "plan_date", "slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_date: Mapped[date] = mapped_column(Date, index=True)
    slot: Mapped[MealSlot] = mapped_column(
        Enum(MealSlot, native_enum=False, length=10), default=MealSlot.DINNER
    )

    recipe_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    # For nights that aren't a recipe: "leftovers", "eating out".
    freeform_title: Mapped[str | None] = mapped_column(String(200))

    servings: Mapped[int] = mapped_column(Integer, default=4)

    # A locked entry survives re-planning; the suggester works around it.
    is_locked: Mapped[bool] = mapped_column(default=False)
    cooked_at: Mapped[datetime | None] = mapped_column()
    # Set when the cook says the meal didn't happen. The row stays so the week
    # still has a history; nothing is debited.
    skipped_at: Mapped[datetime | None] = mapped_column()

    recipe: Mapped[Recipe | None] = relationship()

    def __repr__(self) -> str:
        return f"<MealPlanEntry {self.plan_date} {self.slot}>"


class ShoppingListStatus(enum.StrEnum):
    DRAFT = "draft"  # still being assembled; audit may not have run
    ACTIVE = "active"  # finalized, you're shopping it
    COMPLETED = "completed"  # reconciled against a receipt
    ARCHIVED = "archived"


class ShoppingList(Base, OwnedMixin, TimestampMixin):
    __tablename__ = "shopping_list"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="Weekly shop")
    status: Mapped[ShoppingListStatus] = mapped_column(
        Enum(ShoppingListStatus, native_enum=False, length=12),
        default=ShoppingListStatus.DRAFT,
        index=True,
    )
    shop_date: Mapped[date | None] = mapped_column(Date)
    covers_from: Mapped[date | None] = mapped_column(Date)
    covers_to: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[ShoppingListItem]] = relationship(
        back_populates="shopping_list", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ShoppingList {self.id} {self.name!r} {self.status}>"


class ItemSource(enum.StrEnum):
    RECIPE = "recipe"  # required by a planned meal
    RESTOCK = "restock"  # below its restock threshold
    MANUAL = "manual"  # you added it yourself


class ShoppingListItem(Base):
    __tablename__ = "shopping_list_item"
    __table_args__ = (Index("ix_shopping_item_list", "shopping_list_id", "aisle"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shopping_list_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_list.id", ondelete="CASCADE"), index=True
    )
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="SET NULL"), index=True
    )
    # Free-text fallback for things with no canonical entry (paper towels).
    label: Mapped[str] = mapped_column(String(200))

    # What the plan actually requires, in base units.
    needed_qty: Mapped[float] = mapped_column(Float, default=0.0)
    needed_unit: Mapped[str | None] = mapped_column(String(16))

    # What you'd actually buy, rounded up to a real package: needing 300 g of
    # rice means buying one 1 kg bag, not "300 g of rice".
    purchase_qty: Mapped[float | None] = mapped_column(Float)
    purchase_unit: Mapped[str | None] = mapped_column(String(32))
    purchase_note: Mapped[str | None] = mapped_column(String(200))

    source: Mapped[ItemSource] = mapped_column(
        Enum(ItemSource, native_enum=False, length=10), default=ItemSource.RECIPE
    )
    # Which planned recipes drove this, for "why is this on my list?".
    driven_by: Mapped[str | None] = mapped_column(String(500))

    aisle: Mapped[str | None] = mapped_column(String(60))
    is_checked: Mapped[bool] = mapped_column(default=False)
    # Ticked and written to the ledger from this list. A rebuild keeps the mark
    # so the same line cannot be purchased twice, and a later receipt can see it.
    is_stocked: Mapped[bool] = mapped_column(default=False)
    # Taken off the list. Survives the rebuild that would otherwise put it back.
    is_skipped: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    shopping_list: Mapped[ShoppingList] = relationship(back_populates="items")
    ingredient: Mapped[CanonicalIngredient | None] = relationship()

    def __repr__(self) -> str:
        return f"<ShoppingListItem {self.label!r} x{self.purchase_qty}>"
