"""The ingredient spine: canonical ingredients and the learned alias table."""

from __future__ import annotations

import enum

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, TimestampMixin


class TrackingMode(enum.StrEnum):
    """How an ingredient's level is represented.

    The distinction matters because the two degrade differently: you know you
    have four eggs, but you only ever *estimate* how much flour is in the bag,
    and that estimate gets worse every time you bake.
    """

    DISCRETE = "discrete"  # countable units; depletes exactly, stays confident
    BULK = "bulk"  # fraction of a nominal package; estimated, decays


class IngredientCategory(enum.StrEnum):
    PRODUCE = "produce"
    MEAT = "meat"
    SEAFOOD = "seafood"
    DAIRY = "dairy"
    BAKERY = "bakery"
    GRAIN = "grain"
    LEGUME = "legume"
    CANNED = "canned"
    CONDIMENT = "condiment"
    SPICE = "spice"
    OIL = "oil"
    SWEETENER = "sweetener"
    BAKING = "baking"
    FROZEN = "frozen"
    BEVERAGE = "beverage"
    OTHER = "other"


class CanonicalIngredient(Base, TimestampMixin):
    """One real-world foodstuff, normalized.

    Everything -- recipe lines, receipt lines, pantry rows -- resolves to one of
    these. The unit-bridging fields are what let a recipe asking for "2 cups
    flour" reconcile with a receipt reading "GOLD MEDAL AP FLOUR 5LB".
    """

    __tablename__ = "canonical_ingredient"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    plural_name: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[IngredientCategory] = mapped_column(
        Enum(IngredientCategory, native_enum=False, length=20),
        default=IngredientCategory.OTHER,
        index=True,
    )
    aisle: Mapped[str | None] = mapped_column(String(60))

    tracking_mode: Mapped[TrackingMode] = mapped_column(
        Enum(TrackingMode, native_enum=False, length=10), default=TrackingMode.BULK
    )

    # Canonical storage unit: "g", "ml", or "count".
    base_unit: Mapped[str] = mapped_column(String(16), default="g")

    # Bridges volume and mass. Without this, "2 cups flour" and "5 lb flour"
    # are incommensurable and the whole reconciliation breaks down.
    density_g_per_ml: Mapped[float | None] = mapped_column(Float)

    # Bridges count and mass: 1 egg ~= 50 g, 1 onion ~= 150 g. Needed whenever a
    # recipe counts something the store sells by weight.
    grams_per_count: Mapped[float | None] = mapped_column(Float)

    # What a single purchasable package looks like, for rounding shopping
    # quantities to something you can actually buy.
    package_qty: Mapped[float | None] = mapped_column(Float)
    package_unit: Mapped[str | None] = mapped_column(String(16))

    shelf_life_days: Mapped[int | None] = mapped_column(Integer)

    # Staples (salt, pepper, cooking oil) are assumed present and are omitted
    # from both "missing ingredients" and the shopping list unless explicitly
    # marked out. Treating them like tracked items makes every recipe look
    # unmakeable and every list ten items too long.
    is_staple: Mapped[bool] = mapped_column(default=False)

    notes: Mapped[str | None] = mapped_column(String(500))

    aliases: Mapped[list[IngredientAlias]] = relationship(
        back_populates="ingredient", cascade="all, delete-orphan"
    )

    # There is deliberately no `pantry_item` relationship back from the catalog.
    # An ingredient is shared by every user, so a reverse link would either
    # return some arbitrary user's level or invite a cross-user aggregate.
    # Pantry rows are always reached through a user-scoped query instead.

    def __repr__(self) -> str:
        return f"<CanonicalIngredient {self.id} {self.name!r}>"


class AliasSource(enum.StrEnum):
    SEED = "seed"  # shipped with the app
    RECIPE = "recipe"  # learned from a recipe ingredient line
    RECEIPT = "receipt"  # learned from a receipt line (store abbreviations)
    MANUAL = "manual"  # the user explicitly corrected a match


class IngredientAlias(Base, TimestampMixin):
    """Free text seen in the wild, mapped to a canonical ingredient.

    This table is what stops the app being tedious. Every correction the user
    makes -- "GV EVOO 25.5Z" is olive oil -- is remembered, so each receipt from
    a given store needs less fixing than the one before it.
    """

    __tablename__ = "ingredient_alias"
    __table_args__ = (
        UniqueConstraint("normalized_text", "ingredient_id", name="uq_alias_text_ingredient"),
        Index("ix_alias_lookup", "normalized_text"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="CASCADE"), index=True
    )
    # Lowercased, punctuation-stripped form used for lookup.
    normalized_text: Mapped[str] = mapped_column(String(200))
    # What was actually seen, kept for debugging bad matches.
    raw_text: Mapped[str] = mapped_column(String(200))
    source: Mapped[AliasSource] = mapped_column(
        Enum(AliasSource, native_enum=False, length=10), default=AliasSource.SEED
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # A user-confirmed alias outranks a guessed one; repeated confirmation
    # promotes it further.
    times_confirmed: Mapped[int] = mapped_column(Integer, default=0)

    ingredient: Mapped[CanonicalIngredient] = relationship(back_populates="aliases")

    def __repr__(self) -> str:
        return f"<IngredientAlias {self.normalized_text!r} -> {self.ingredient_id}>"
