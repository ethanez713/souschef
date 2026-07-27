"""Recipes and their ingredient lines."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from souschef.models.ingredient import CanonicalIngredient


class RecipeSource(enum.StrEnum):
    MANUAL = "manual"
    URL = "url"  # generic site via recipe-scrapers
    NYT = "nyt"  # NYT Cooking, imported on demand
    SPOONACULAR = "spoonacular"  # discovery result, saved


class Recipe(Base, TimestampMixin):
    __tablename__ = "recipe"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_recipe_source_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    source: Mapped[RecipeSource] = mapped_column(
        Enum(RecipeSource, native_enum=False, length=12), default=RecipeSource.MANUAL
    )
    source_url: Mapped[str | None] = mapped_column(String(1000))
    # Stable id at the source, so re-importing updates rather than duplicates.
    external_id: Mapped[str | None] = mapped_column(String(120))

    author: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(1000))

    servings: Mapped[int] = mapped_column(Integer, default=4)
    total_minutes: Mapped[int | None] = mapped_column(Integer)

    # Drives the familiarity bonus and the variety penalty in suggestion
    # scoring: the app should favour food you know but not propose it weekly.
    times_cooked: Mapped[int] = mapped_column(Integer, default=0)
    last_cooked_at: Mapped[datetime | None] = mapped_column()
    rating: Mapped[int | None] = mapped_column(Integer)  # 1-5
    is_favorite: Mapped[bool] = mapped_column(default=False)
    is_archived: Mapped[bool] = mapped_column(default=False)

    notes: Mapped[str | None] = mapped_column(Text)

    ingredients: Mapped[list[RecipeIngredient]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeIngredient.position",
    )

    def __repr__(self) -> str:
        return f"<Recipe {self.id} {self.title!r}>"


class RecipeIngredient(Base):
    """One ingredient line, keeping both the original text and the parse.

    ``raw_text`` is never discarded: it is what gets re-displayed to the user and
    what a re-parse runs against when the matcher improves.
    """

    __tablename__ = "recipe_ingredient"
    __table_args__ = (Index("ix_recipe_ingredient_lookup", "recipe_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipe.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    raw_text: Mapped[str] = mapped_column(String(500))

    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="SET NULL"), index=True
    )
    # Confidence of the raw_text -> canonical match; low values get surfaced for
    # review rather than silently trusted.
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    preparation: Mapped[str | None] = mapped_column(String(200))  # "finely chopped"

    # "Salt and pepper to taste" -- has no meaningful quantity and must never
    # block a recipe from being cookable or land on a shopping list.
    is_optional: Mapped[bool] = mapped_column(default=False)
    quantity_is_vague: Mapped[bool] = mapped_column(default=False)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
    ingredient: Mapped[CanonicalIngredient | None] = relationship()

    def __repr__(self) -> str:
        return f"<RecipeIngredient {self.raw_text!r}>"
