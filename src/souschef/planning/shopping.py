"""Turn a week's meal plan into a shopping list.

The pipeline:

    planned meals -> ingredient requirements -> subtract pantry
      -> round up to purchasable packages -> group by aisle

Two details do most of the work in making the output usable:

**Round to real packages.** Needing 300 g of rice means buying a 1 kg bag, not
"300 g of rice". A list you cannot execute in a shop is not a shopping list.

**Say why each item is there.** Every line records which planned meals drove it,
so "why am I buying buttermilk?" is answerable in the aisle -- and so dropping a
meal can correctly drop its ingredients.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from souschef.core.inventory import physical_of
from souschef.core.recipes import recipe_requirements
from souschef.core.units import UnitConversionError, humanize, to_base
from souschef.models import (
    CanonicalIngredient,
    MealPlanEntry,
    PantryItem,
    Recipe,
    ShoppingList,
    ShoppingListItem,
    ShoppingListStatus,
)
from souschef.models.planning import ItemSource

DEFAULT_AISLE = "Other"


@dataclass
class NeedLine:
    ingredient: CanonicalIngredient
    needed: float  # base units required by the plan
    have: float  # believed pantry level
    driven_by: list[str] = field(default_factory=list)
    source: ItemSource = ItemSource.RECIPE

    @property
    def shortfall(self) -> float:
        return max(0.0, self.needed - self.have)


def collect_requirements(
    session: Session,
    entries: list[MealPlanEntry],
) -> dict[int, NeedLine]:
    """Sum what every planned meal needs, remembering which meals drove each item."""
    needs: dict[int, NeedLine] = {}

    for entry in entries:
        if entry.recipe is None or entry.cooked_at is not None:
            continue
        requirements = recipe_requirements(session, entry.recipe, servings=entry.servings)

        for req in requirements.requirements:
            # Staples are assumed present; optional extras shouldn't pad a list.
            if req.is_staple or req.is_optional:
                continue
            line = needs.get(req.ingredient.id)
            if line is None:
                line = NeedLine(ingredient=req.ingredient, needed=0.0, have=0.0)
                needs[req.ingredient.id] = line
            line.needed += req.quantity
            if entry.recipe.title not in line.driven_by:
                line.driven_by.append(entry.recipe.title)

    return needs


def apply_pantry(
    session: Session, user_id: int, needs: dict[int, NeedLine]
) -> dict[int, NeedLine]:
    """Fill in believed pantry levels for each needed ingredient."""
    if not needs:
        return needs
    rows = session.scalars(
        select(PantryItem).where(
            PantryItem.user_id == user_id,
            PantryItem.ingredient_id.in_(needs.keys()),
        )
    ).all()
    for item in rows:
        needs[item.ingredient_id].have = item.quantity
    return needs


def add_restock_candidates(
    session: Session, user_id: int, needs: dict[int, NeedLine]
) -> dict[int, NeedLine]:
    """Add anything sitting below its restock floor, even if unplanned."""
    rows = session.execute(
        select(PantryItem, CanonicalIngredient)
        .join(CanonicalIngredient, PantryItem.ingredient_id == CanonicalIngredient.id)
        .where(PantryItem.user_id == user_id, PantryItem.restock_threshold.isnot(None))
    ).all()

    for item, ingredient in rows:
        if ingredient.id in needs:
            continue
        if item.quantity >= (item.restock_threshold or 0):
            continue
        needs[ingredient.id] = NeedLine(
            ingredient=ingredient,
            needed=item.restock_threshold or 0.0,
            have=item.quantity,
            driven_by=["running low"],
            source=ItemSource.RESTOCK,
        )
    return needs


def to_purchase_quantity(
    ingredient: CanonicalIngredient, shortfall: float
) -> tuple[float, str, str | None]:
    """Round a shortfall up to something you can actually put in a basket.

    Returns ``(quantity, unit, note)``. Falls back to the raw base-unit amount
    when no package size is known, which is honest rather than inventing one.
    """
    if shortfall <= 0:
        return 0.0, ingredient.base_unit, None

    if not ingredient.package_qty or not ingredient.package_unit:
        return shortfall, ingredient.base_unit, None

    try:
        package_base = to_base(
            ingredient.package_qty, ingredient.package_unit, physical_of(ingredient)
        )
    except UnitConversionError:
        return shortfall, ingredient.base_unit, None

    if package_base <= 0:
        return shortfall, ingredient.base_unit, None

    packages = math.ceil(shortfall / package_base)

    # For countable things the package *is* the unit, so say "3 onions".
    if ingredient.base_unit == "count" and ingredient.package_qty == 1:
        return float(packages), "count", None

    note = f"needs {humanize(shortfall, ingredient.base_unit)}"
    unit_label = f"x {humanize(ingredient.package_qty, ingredient.package_unit)}"
    return float(packages), unit_label, note


def build_shopping_list(
    session: Session,
    user_id: int,
    *,
    covers_from: date,
    covers_to: date,
    name: str = "Weekly shop",
    include_restock: bool = True,
) -> ShoppingList:
    """Generate a shopping list for a date range of one user's planned meals."""
    entries = session.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe).selectinload(Recipe.ingredients))
        .where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.plan_date >= covers_from,
            MealPlanEntry.plan_date <= covers_to,
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()

    needs = collect_requirements(session, entries)
    needs = apply_pantry(session, user_id, needs)
    if include_restock:
        needs = add_restock_candidates(session, user_id, needs)

    # One live list per kitchen: replace whatever draft is hanging around so
    # generating never piles up stale copies, and carry ticks across the rebuild
    # so checking off eggs then adding a meal doesn't uncheck them.
    existing_lists = session.scalars(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(
            ShoppingList.user_id == user_id,
            ShoppingList.status != ShoppingListStatus.ARCHIVED,
        )
    ).all()
    already_checked = {
        item.ingredient_id
        for lst in existing_lists
        for item in lst.items
        if item.is_checked and item.ingredient_id is not None
    }
    for lst in existing_lists:
        session.delete(lst)
    session.flush()

    shopping_list = ShoppingList(
        user_id=user_id,
        name=name,
        status=ShoppingListStatus.DRAFT,
        covers_from=covers_from,
        covers_to=covers_to,
    )
    session.add(shopping_list)
    session.flush()

    ordered = sorted(
        (line for line in needs.values() if line.shortfall > 0),
        key=lambda line: (line.ingredient.aisle or DEFAULT_AISLE, line.ingredient.name),
    )

    for position, line in enumerate(ordered):
        quantity, unit, note = to_purchase_quantity(line.ingredient, line.shortfall)
        shopping_list.items.append(
            ShoppingListItem(
                ingredient_id=line.ingredient.id,
                label=line.ingredient.name,
                needed_qty=line.shortfall,
                needed_unit=line.ingredient.base_unit,
                purchase_qty=quantity,
                purchase_unit=unit,
                purchase_note=note,
                source=line.source,
                driven_by=", ".join(line.driven_by[:4]) or None,
                aisle=line.ingredient.aisle or DEFAULT_AISLE,
                is_checked=line.ingredient.id in already_checked,
                sort_order=position,
            )
        )

    session.flush()
    return shopping_list


def requirements_for_audit(
    session: Session, user_id: int, covers_from: date, covers_to: date
) -> dict[int, float]:
    """Plan requirements in the shape the audit engine expects.

    Deliberately includes items the pantry may already cover: the audit's job is
    to decide which *beliefs* to verify, and an item we think we have plenty of
    is exactly the kind of belief that can be wrong.
    """
    entries = session.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe).selectinload(Recipe.ingredients))
        .where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.plan_date >= covers_from,
            MealPlanEntry.plan_date <= covers_to,
        )
    ).all()
    needs = collect_requirements(session, entries)
    return {ingredient_id: line.needed for ingredient_id, line in needs.items()}
