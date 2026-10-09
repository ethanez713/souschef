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
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from souschef.core.inventory import (
    INFERRED_PURCHASE_CONFIDENCE,
    physical_of,
    record_event,
)
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.recipes import recipe_requirements
from souschef.core.units import UnitConversionError, dimension_of, humanize, to_base
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    MealPlanEntry,
    PantryItem,
    Recipe,
    ShoppingList,
    ShoppingListItem,
    ShoppingListStatus,
)
from souschef.models.ingredient import IngredientCategory
from souschef.models.planning import ItemSource
from souschef.planning.suggest import nonoptional_unmatched

DEFAULT_AISLE = "Other"


@dataclass
class NeedLine:
    ingredient: CanonicalIngredient
    needed: float  # base units required by the plan
    have: float  # believed pantry level
    driven_by: list[str] = field(default_factory=list)
    source: ItemSource = ItemSource.RECIPE
    # A matched line whose recipe gave no amount. Cleared if any line for the
    # same ingredient does state one — that number is the shortfall.
    unknown_amount: bool = False

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
        if (
            entry.recipe is None
            or entry.cooked_at is not None
            or entry.skipped_at is not None
        ):
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
            if req.quantity > 0:
                line.needed += req.quantity
                line.unknown_amount = False
            elif req.is_vague and line.needed <= 0:
                line.unknown_amount = True
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

    # A need within 1% of a whole package is that package: a stored 946 ml carton
    # against a 946.35 ml conversion must not buy a second carton.
    packages = max(1, math.ceil(shortfall / package_base - 0.01))

    # For countable things the package *is* the unit, so say "3 onions".
    if ingredient.base_unit == "count" and ingredient.package_qty == 1:
        return float(packages), "count", None

    note = f"needs {humanize(shortfall, ingredient.base_unit, us=True)}"
    unit_label = f"x {humanize(ingredient.package_qty, ingredient.package_unit, us=True)}"
    return float(packages), unit_label, note


def _one_package(ingredient: CanonicalIngredient) -> tuple[float, str, str]:
    """One purchasable unit, when the recipe named the food but not the amount."""
    note = "amount wasn't on the recipe"
    if ingredient.package_qty and ingredient.package_unit:
        return 1.0, f"x {humanize(ingredient.package_qty, ingredient.package_unit, us=True)}", note
    return 1.0, ingredient.base_unit, note


def _on_the_list(line: NeedLine) -> bool:
    if line.shortfall > 0:
        return True
    return line.unknown_amount and line.have <= 0 and line.needed <= 0


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
    # generating never piles up stale copies. Ticks, manual lines, skips and
    # "already added to the pantry" marks are carried across, and so is the
    # original build time — receipt review uses it to avoid counting a shop twice.
    existing_lists = session.scalars(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(
            ShoppingList.user_id == user_id,
            ShoppingList.status != ShoppingListStatus.ARCHIVED,
        )
    ).all()
    carried = _carry_forward(existing_lists)
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
    if carried.built_at is not None:
        shopping_list.created_at = carried.built_at
    session.add(shopping_list)
    session.flush()

    ordered = sorted(
        (line for line in needs.values() if _on_the_list(line)),
        key=lambda line: (line.ingredient.aisle or DEFAULT_AISLE, line.ingredient.name),
    )

    position = 0
    generated_ids: set[int] = set()
    for line in ordered:
        if line.ingredient.id in carried.skipped_ids:
            continue
        if line.shortfall > 0:
            quantity, unit, note = to_purchase_quantity(line.ingredient, line.shortfall)
        else:
            quantity, unit, note = _one_package(line.ingredient)
        stocked = line.ingredient.id in carried.stocked_ids
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
                is_checked=stocked or line.ingredient.id in carried.checked_ids,
                is_stocked=stocked,
                is_skipped=False,
                sort_order=position,
            )
        )
        generated_ids.add(line.ingredient.id)
        position += 1

    _append_manuals(shopping_list, carried, generated_ids)
    _append_skip_markers(session, shopping_list, carried)

    session.flush()
    return shopping_list


class ShoppingError(ValueError):
    """A shopping-list edit the page can show as a form message."""


@dataclass
class _CarriedManual:
    ingredient_id: int | None
    label: str
    needed_qty: float
    needed_unit: str | None
    purchase_qty: float | None
    purchase_unit: str | None
    purchase_note: str | None
    driven_by: str | None
    aisle: str | None
    is_checked: bool
    is_stocked: bool


@dataclass
class _Carried:
    built_at: datetime | None = None
    checked_ids: set[int] = field(default_factory=set)
    stocked_ids: set[int] = field(default_factory=set)
    skipped_ids: set[int] = field(default_factory=set)
    skipped_labels: set[str] = field(default_factory=set)
    manuals: list[_CarriedManual] = field(default_factory=list)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _carry_forward(lists: list[ShoppingList]) -> _Carried:
    carried = _Carried()
    for lst in lists:
        if lst.created_at is not None:
            built = _aware(lst.created_at)
            if carried.built_at is None or built < carried.built_at:
                carried.built_at = built
        for item in lst.items:
            if item.source == ItemSource.MANUAL and not item.is_skipped:
                carried.manuals.append(
                    _CarriedManual(
                        ingredient_id=item.ingredient_id,
                        label=item.label,
                        needed_qty=item.needed_qty,
                        needed_unit=item.needed_unit,
                        purchase_qty=item.purchase_qty,
                        purchase_unit=item.purchase_unit,
                        purchase_note=item.purchase_note,
                        driven_by=item.driven_by,
                        aisle=item.aisle,
                        is_checked=item.is_checked,
                        is_stocked=item.is_stocked,
                    )
                )
                continue
            if item.is_skipped:
                if item.ingredient_id is not None:
                    carried.skipped_ids.add(item.ingredient_id)
                else:
                    carried.skipped_labels.add(item.label.casefold())
                continue
            if item.ingredient_id is None:
                continue
            if item.is_checked:
                carried.checked_ids.add(item.ingredient_id)
            if item.is_stocked:
                carried.stocked_ids.add(item.ingredient_id)

    # Putting something back on the list by hand undoes an earlier skip.
    for manual in carried.manuals:
        if manual.ingredient_id is not None:
            carried.skipped_ids.discard(manual.ingredient_id)
        carried.skipped_labels.discard(manual.label.casefold())
    return carried


def _append_manuals(
    shopping_list: ShoppingList, carried: _Carried, generated_ids: set[int]
) -> None:
    for manual in carried.manuals:
        if manual.ingredient_id is not None and manual.ingredient_id in generated_ids:
            for item in shopping_list.items:
                if item.ingredient_id == manual.ingredient_id and not item.is_skipped:
                    item.is_checked = item.is_checked or manual.is_checked
                    item.is_stocked = item.is_stocked or manual.is_stocked
            continue
        shopping_list.items.append(
            ShoppingListItem(
                ingredient_id=manual.ingredient_id,
                label=manual.label,
                needed_qty=manual.needed_qty,
                needed_unit=manual.needed_unit,
                purchase_qty=manual.purchase_qty,
                purchase_unit=manual.purchase_unit,
                purchase_note=manual.purchase_note,
                source=ItemSource.MANUAL,
                driven_by=manual.driven_by,
                aisle=manual.aisle or DEFAULT_AISLE,
                is_checked=manual.is_checked,
                is_stocked=manual.is_stocked,
                is_skipped=False,
            )
        )


def _append_skip_markers(session: Session, shopping_list: ShoppingList, carried: _Carried) -> None:
    present = {
        item.ingredient_id
        for item in shopping_list.items
        if item.ingredient_id is not None and not item.is_skipped
    }
    labels = {item.label.casefold() for item in shopping_list.items if not item.is_skipped}
    for ingredient_id in carried.skipped_ids:
        if ingredient_id in present:
            continue
        ingredient = session.get(CanonicalIngredient, ingredient_id)
        if ingredient is None:
            continue
        shopping_list.items.append(
            ShoppingListItem(
                ingredient_id=ingredient_id,
                label=ingredient.name,
                needed_qty=0,
                source=ItemSource.RECIPE,
                aisle=ingredient.aisle or DEFAULT_AISLE,
                is_checked=False,
                is_stocked=False,
                is_skipped=True,
            )
        )
    for label in sorted(carried.skipped_labels):
        if label in labels:
            continue
        shopping_list.items.append(
            ShoppingListItem(
                label=label,
                needed_qty=0,
                source=ItemSource.MANUAL,
                aisle=DEFAULT_AISLE,
                is_checked=False,
                is_stocked=False,
                is_skipped=True,
            )
        )


def live_list(session: Session, user_id: int) -> ShoppingList | None:
    """The kitchen's current list, if one has been built."""
    return session.scalar(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(
            ShoppingList.user_id == user_id,
            ShoppingList.status != ShoppingListStatus.ARCHIVED,
        )
        .order_by(ShoppingList.id.desc())
    )


def listed_purchase(
    ingredient: CanonicalIngredient,
    purchase_qty: float | None,
    purchase_unit: str | None,
) -> tuple[float, str]:
    """What a ticked line actually puts in the basket.

    Packaged lines store a package count (``2 x 907 g``). The ledger wants the
    whole packages, not the recipe's shortfall.
    """
    unit = (purchase_unit or "").strip()
    qty = float(purchase_qty or 0)
    if unit.startswith("x ") and ingredient.package_qty and ingredient.package_unit:
        packages = qty if qty > 0 else 1.0
        return packages * float(ingredient.package_qty), ingredient.package_unit
    if qty > 0:
        return qty, unit or ingredient.base_unit
    if ingredient.package_qty and ingredient.package_unit:
        return float(ingredient.package_qty), ingredient.package_unit
    return 1.0, ingredient.base_unit


def _exact_ingredient(session: Session, name: str) -> CanonicalIngredient | None:
    match = IngredientMatcher(session).match(name)
    if match.method not in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT):
        return None
    if match.ingredient_id is None:
        return None
    return session.get(CanonicalIngredient, match.ingredient_id)


def _create_adhoc(session: Session, name: str, unit: str | None) -> CanonicalIngredient:
    clean = name.strip()
    existing = session.scalar(
        select(CanonicalIngredient).where(func.lower(CanonicalIngredient.name) == clean.casefold())
    )
    if existing is not None:
        return existing
    base = "count"
    cleaned = (unit or "").strip()
    if cleaned and not cleaned.startswith("x "):
        try:
            dim = dimension_of(cleaned)
        except UnitConversionError:
            dim = None
        base = {"mass": "g", "volume": "ml"}.get(str(dim), "count")
    ingredient = CanonicalIngredient(
        name=clean, category=IngredientCategory.OTHER, base_unit=base
    )
    session.add(ingredient)
    session.flush()
    return ingredient


def _ingredient_for_stock(session: Session, item: ShoppingListItem) -> CanonicalIngredient | None:
    if item.ingredient_id is not None:
        return session.get(CanonicalIngredient, item.ingredient_id)
    found = _exact_ingredient(session, item.label)
    if found is not None:
        return found
    if not item.label.strip():
        return None
    return _create_adhoc(session, item.label, item.purchase_unit)


def stock_ticked_items(session: Session, user_id: int, shopping_list: ShoppingList) -> list[str]:
    """Write one low-confidence purchase per ticked line that is not already stocked.

    The amount is the package on the line. The line is marked stocked so a second
    tap, or the rebuild that follows, cannot buy it again.
    """
    added: list[str] = []
    for item in list(shopping_list.items):
        if item.is_skipped or not item.is_checked or item.is_stocked:
            continue
        ingredient = _ingredient_for_stock(session, item)
        if ingredient is None or ingredient.is_staple:
            item.is_stocked = True
            continue
        qty, unit = listed_purchase(ingredient, item.purchase_qty, item.purchase_unit)
        try:
            record_event(
                session,
                user_id,
                ingredient,
                reason=EventReason.PURCHASE,
                delta=qty,
                unit=unit,
                event_confidence=INFERRED_PURCHASE_CONFIDENCE,
                source_ref=f"shopping:{shopping_list.id}",
                note=f"added from shopping list: {item.label}",
            )
        except UnitConversionError as exc:
            raise ShoppingError(str(exc)) from exc
        item.is_stocked = True
        if item.ingredient_id is None:
            item.ingredient_id = ingredient.id
        added.append(ingredient.name)
    session.flush()
    return added


def add_manual_item(
    session: Session,
    shopping_list: ShoppingList,
    name: str,
    amount: float | None = None,
    unit: str | None = None,
) -> ShoppingListItem:
    """Add a free-text line. A catalog hit takes that ingredient's aisle."""
    clean = name.strip()
    if not clean:
        raise ShoppingError("Enter a name.")
    if amount is not None and amount <= 0:
        raise ShoppingError("Amount has to be more than zero.")

    ingredient = _exact_ingredient(session, clean)
    if ingredient is not None and ingredient.is_staple:
        raise ShoppingError(
            f"{ingredient.name} is assumed on hand, so it stays off the list."
        )

    label = ingredient.name if ingredient is not None else clean
    catalog_aisle = ingredient.aisle if ingredient is not None else None
    aisle = catalog_aisle or DEFAULT_AISLE

    for item in shopping_list.items:
        if not item.is_skipped and item.label.casefold() == label.casefold():
            raise ShoppingError(f"{label} is already on the list.")

    if ingredient is not None and amount is None:
        purchase_qty, purchase_unit, purchase_note = _one_package(ingredient)
        needed_qty = 0.0
        needed_unit = ingredient.base_unit
    elif amount is not None:
        purchase_qty = amount
        purchase_unit = (unit or "").strip() or (
            ingredient.base_unit if ingredient is not None else "count"
        )
        purchase_note = None
        needed_qty = amount
        needed_unit = purchase_unit
    else:
        purchase_qty = None
        purchase_unit = None
        purchase_note = None
        needed_qty = 0.0
        needed_unit = None

    revived = None
    for item in shopping_list.items:
        if not item.is_skipped:
            continue
        same_ingredient = ingredient is not None and item.ingredient_id == ingredient.id
        same_label = item.label.casefold() == label.casefold()
        if same_ingredient or same_label:
            revived = item
            break
    if revived is not None:
        revived.ingredient_id = ingredient.id if ingredient is not None else None
        revived.label = label
        revived.needed_qty = needed_qty
        revived.needed_unit = needed_unit
        revived.purchase_qty = purchase_qty
        revived.purchase_unit = purchase_unit
        revived.purchase_note = purchase_note
        revived.source = ItemSource.MANUAL
        revived.driven_by = "added by you"
        revived.aisle = aisle
        revived.is_checked = False
        revived.is_stocked = False
        revived.is_skipped = False
        session.flush()
        return revived

    item = ShoppingListItem(
        ingredient_id=ingredient.id if ingredient is not None else None,
        label=label,
        needed_qty=needed_qty,
        needed_unit=needed_unit,
        purchase_qty=purchase_qty,
        purchase_unit=purchase_unit,
        purchase_note=purchase_note,
        source=ItemSource.MANUAL,
        driven_by="added by you",
        aisle=aisle,
        is_checked=False,
        is_stocked=False,
        is_skipped=False,
    )
    shopping_list.items.append(item)
    session.flush()
    return item


def unnamed_recipe_lines(
    session: Session, user_id: int, covers_from: date, covers_to: date
) -> list[tuple[int, str, str]]:
    """Non-optional unidentified lines on planned recipes, once each.

    Each tuple is ``(recipe_id, title, raw_text)``.
    """
    entries = session.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe).selectinload(Recipe.ingredients))
        .where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.plan_date >= covers_from,
            MealPlanEntry.plan_date <= covers_to,
        )
        .order_by(MealPlanEntry.plan_date, MealPlanEntry.id)
    ).all()
    seen: set[tuple[int, str]] = set()
    lines: list[tuple[int, str, str]] = []
    for entry in entries:
        if entry.recipe is None or entry.cooked_at is not None or entry.skipped_at is not None:
            continue
        requirements = recipe_requirements(session, entry.recipe, servings=entry.servings)
        for text in nonoptional_unmatched(entry.recipe, requirements):
            key = (entry.recipe.id, text)
            if key in seen:
                continue
            seen.add(key)
            lines.append((entry.recipe.id, entry.recipe.title, text))
    return lines


def ingredients_stocked_since(session: Session, user_id: int, since: datetime) -> set[int]:
    """Ingredients bought from a shopping list at or after ``since``.

    ``since`` is when the live list was first built. A receipt reviewed later
    unticks those lines so the same shop is not counted twice.
    """
    bound = _aware(since)
    rows = session.scalars(
        select(InventoryEvent).where(
            InventoryEvent.user_id == user_id,
            InventoryEvent.reason == EventReason.PURCHASE,
            InventoryEvent.source_ref.like("shopping:%"),
        )
    ).all()
    found: set[int] = set()
    for event in rows:
        if event.occurred_at is None:
            continue
        if _aware(event.occurred_at) >= bound:
            found.add(event.ingredient_id)
    return found


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
