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
    live_events,
    physical_of,
    record_event,
    save_package_size,
)
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.recipes import recipe_requirements
from souschef.core.units import (
    InputUnitError,
    NeedsPackage,
    UnitConversionError,
    count_noun_measure,
    dimension_of,
    parse_input_unit,
    parse_package_answer,
    resolve_entry,
    to_base,
)
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
NEEDS_NAMING_AISLE = "Needs naming"

# A purchase counted in packages, not a unit of measure. The package size stays
# on the ingredient so a later render can follow the viewer's unit system.
# Older rows stored the formatted size after this marker ("x 907 g (32 oz)").
PACKAGE_COUNT_UNIT = "x"


def is_package_count(unit: str | None) -> bool:
    text = (unit or "").strip()
    return text == PACKAGE_COUNT_UNIT or text.startswith(f"{PACKAGE_COUNT_UNIT} ")


@dataclass
class _UnnamedNeed:
    label: str
    driven_by: list[str]


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

    # The shortfall stays in needed_qty. The package size stays on the ingredient.
    return float(packages), PACKAGE_COUNT_UNIT, None


def _one_package(ingredient: CanonicalIngredient) -> tuple[float, str, str]:
    """One purchasable unit, when the recipe named the food but not the amount."""
    note = "amount wasn't on the recipe"
    if ingredient.package_qty and ingredient.package_unit:
        return 1.0, PACKAGE_COUNT_UNIT, note
    return 1.0, ingredient.base_unit, note


def _on_the_list(line: NeedLine) -> bool:
    if line.shortfall > 0:
        return True
    return line.unknown_amount and line.have <= 0 and line.needed <= 0


def _item_key(ingredient_id: int | None, label: str) -> tuple[str, int | str]:
    if ingredient_id is not None:
        return ("id", ingredient_id)
    return ("label", label.casefold())


def _write_generated(item: ShoppingListItem, line: NeedLine, position: int) -> None:
    if line.shortfall > 0:
        quantity, unit, note = to_purchase_quantity(line.ingredient, line.shortfall)
    else:
        quantity, unit, note = _one_package(line.ingredient)
    item.ingredient_id = line.ingredient.id
    item.label = line.ingredient.name
    item.needed_qty = line.shortfall
    item.needed_unit = line.ingredient.base_unit
    if not item.amount_edited:
        item.purchase_qty = quantity
        item.purchase_unit = unit
        item.purchase_note = note
    item.driven_by = ", ".join(line.driven_by[:4]) or None
    item.aisle = line.ingredient.aisle or DEFAULT_AISLE
    item.sort_order = position
    # A hand-added row stays manual, so a later rebuild will not drop it when
    # the recipe that happened to need the same thing is gone.
    if item.source != ItemSource.MANUAL:
        item.source = line.source


def _write_unnamed(item: ShoppingListItem, line: _UnnamedNeed, position: int) -> None:
    item.ingredient_id = None
    item.label = line.label
    item.needed_qty = 0.0
    item.needed_unit = None
    if not item.amount_edited:
        item.purchase_qty = None
        item.purchase_unit = None
        item.purchase_note = None
    item.driven_by = ", ".join(line.driven_by[:4]) or None
    item.aisle = NEEDS_NAMING_AISLE
    item.sort_order = position
    if item.source != ItemSource.MANUAL:
        item.source = ItemSource.RECIPE


def _unnamed_needs(
    session: Session, user_id: int, covers_from: date, covers_to: date
) -> list[_UnnamedNeed]:
    """One row per distinct casefolded raw line, in plan order.

    The same source as ``unnamed_recipe_lines``: planned, not cooked, not
    skipped, and ``nonoptional_unmatched`` already drops ignored lines.
    """
    grouped: dict[str, _UnnamedNeed] = {}
    for _recipe_id, title, text in unnamed_recipe_lines(session, user_id, covers_from, covers_to):
        key = text.casefold()
        row = grouped.get(key)
        if row is None:
            grouped[key] = _UnnamedNeed(label=text, driven_by=[title])
        elif title not in row.driven_by:
            row.driven_by.append(title)
    return list(grouped.values())


def build_shopping_list(
    session: Session,
    user_id: int,
    *,
    covers_from: date,
    covers_to: date,
    name: str = "Weekly shop",
    include_restock: bool = True,
) -> ShoppingList:
    """Reconcile the kitchen's one live list for a date range of planned meals.

    Rows update in place, keyed by ingredient or by casefolded label when the
    line has none, so a row's id and its checked, stocked and skipped marks
    survive a rebuild. A planned line the matcher could not name is one of
    those label rows, grouped under Needs naming.
    """
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

    needs = collect_requirements(session, entries)  # type: ignore[arg-type]
    needs = apply_pantry(session, user_id, needs)
    if include_restock:
        needs = add_restock_candidates(session, user_id, needs)

    # Same row every time, so receipt review still sees the original build time.
    shopping_list = live_list(session, user_id)
    if shopping_list is None:
        shopping_list = ShoppingList(
            user_id=user_id,
            name=name,
            status=ShoppingListStatus.DRAFT,
            covers_from=covers_from,
            covers_to=covers_to,
        )
        session.add(shopping_list)
    else:
        shopping_list.name = name
        shopping_list.covers_from = covers_from
        shopping_list.covers_to = covers_to

    ordered = sorted(
        (line for line in needs.values() if _on_the_list(line)),
        key=lambda line: (line.ingredient.aisle or DEFAULT_AISLE, line.ingredient.name),
    )

    chosen: dict[tuple[str, int | str], ShoppingListItem] = {}
    for item in sorted(shopping_list.items, key=lambda row: row.id or 0):
        key = _item_key(item.ingredient_id, item.label)
        current = chosen.get(key)
        if current is None or (
            item.source == ItemSource.MANUAL and current.source != ItemSource.MANUAL
        ):
            chosen[key] = item
    keepers = {id(item) for item in chosen.values()}
    for item in list(shopping_list.items):
        if id(item) not in keepers:
            shopping_list.items.remove(item)

    generated: set[tuple[str, int | str]] = set()
    for position, line in enumerate(ordered):
        key = _item_key(line.ingredient.id, line.ingredient.name)
        generated.add(key)
        existing = chosen.get(key)
        if existing is None:
            existing = ShoppingListItem(
                ingredient_id=line.ingredient.id,
                label=line.ingredient.name,
                source=line.source,
                is_checked=False,
                is_stocked=False,
                is_skipped=False,
            )
            shopping_list.items.append(existing)
            chosen[key] = existing
        _write_generated(existing, line, position)

    for position, unnamed in enumerate(_unnamed_needs(session, user_id, covers_from, covers_to)):
        key = _item_key(None, unnamed.label)
        generated.add(key)
        existing = chosen.get(key)
        # A hand-added line with this text stays manual. It is not a recipe
        # row, and a rebuild must not move it or clear the amount they typed.
        if existing is not None and existing.source == ItemSource.MANUAL:
            continue
        if existing is None:
            existing = ShoppingListItem(
                ingredient_id=None,
                label=unnamed.label,
                source=ItemSource.RECIPE,
                is_checked=False,
                is_stocked=False,
                is_skipped=False,
            )
            shopping_list.items.append(existing)
            chosen[key] = existing
        _write_unnamed(existing, unnamed, position)

    # A tick still in the basket outlives the meal that put it there. A stocked
    # line has already been written to the ledger, and a skip only lasts while
    # its need is still generated — leaving either behind would make the next
    # shop start already ticked or already skipped. Manual lines stay.
    for item in list(shopping_list.items):
        if _item_key(item.ingredient_id, item.label) in generated:
            continue
        if item.source == ItemSource.MANUAL:
            continue
        if (
            item.is_checked
            and not item.is_stocked
            and not item.is_skipped
            and item.aisle != NEEDS_NAMING_AISLE
        ):
            continue
        shopping_list.items.remove(item)

    session.flush()
    return shopping_list


class ShoppingError(ValueError):
    """A shopping-list edit the page can show as a form message."""


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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
    if is_package_count(unit) and ingredient.package_qty and ingredient.package_unit:
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
    if cleaned and not is_package_count(cleaned):
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


def stockable(item: ShoppingListItem) -> bool:
    """Whether Add to pantry applies to this ticked row.

    An unnamed recipe line has no ingredient, so ticking it changes nothing in
    the pantry. A hand-added line with no catalog match still stocks: that is
    what creates the ingredient.
    """
    return item.ingredient_id is not None or item.source == ItemSource.MANUAL


def stock_ticked_items(
    session: Session, user_id: int, shopping_list: ShoppingList
) -> tuple[list[str], list[int]]:
    """Write one low-confidence purchase per ticked line that is not already stocked.

    Unnamed recipe lines are skipped. The amount is the package on the line.
    The line is marked stocked so a second tap, or the rebuild that follows,
    cannot buy it again. The event ids are the purchases just written.
    """
    added: list[str] = []
    event_ids: list[int] = []
    for item in list(shopping_list.items):
        if item.is_skipped or not item.is_checked or item.is_stocked:
            continue
        if not stockable(item):
            continue
        ingredient = _ingredient_for_stock(session, item)
        if ingredient is None or ingredient.is_staple:
            item.is_stocked = True
            continue
        qty, unit = listed_purchase(ingredient, item.purchase_qty, item.purchase_unit)
        try:
            event = record_event(
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
        event_ids.append(event.id)
    session.flush()
    return added, event_ids


def _amount_only(
    ingredient: CanonicalIngredient | None, amount: float | None
) -> tuple[
    CanonicalIngredient | None, float | None, str | None, str | None, float, str | None
]:
    if ingredient is not None and amount is None:
        purchase_qty, purchase_unit, purchase_note = _one_package(ingredient)
        return ingredient, purchase_qty, purchase_unit, purchase_note, 0.0, ingredient.base_unit
    if amount is not None:
        purchase_unit = ingredient.base_unit if ingredient is not None else "count"
        return ingredient, amount, purchase_unit, None, amount, purchase_unit
    return ingredient, None, None, None, 0.0, None


def _manual_purchase(
    session: Session,
    ingredient: CanonicalIngredient | None,
    name: str,
    amount: float | None,
    unit: str | None,
    package_qty: str | None,
    package_unit: str | None,
) -> tuple[
    CanonicalIngredient | None, float | None, str | None, str | None, float, str | None
]:
    typed = (unit or "").strip()
    noun = ingredient.count_noun if ingredient is not None else None
    if count_noun_measure(typed, noun):
        typed = "count"
    if not typed:
        return _amount_only(ingredient, amount)

    pack = parse_input_unit(typed)[1]
    # A blank amount beside a real measure still means one of the usual package.
    # The unit box is not how that line is counted. An unknown word still errors,
    # because parse_input_unit already ran.
    if pack is None and amount is None:
        return _amount_only(ingredient, None)

    count = amount if amount is not None else 1.0
    try:
        answer = parse_package_answer(package_qty or "", package_unit or "")
    except InputUnitError as err:
        raise NeedsPackage(pack or "package", str(err)) from err
    known = bool(ingredient is not None and ingredient.package_qty and ingredient.package_unit)
    if answer is None and pack and pack != "dozen" and not known:
        raise NeedsPackage(pack)
    if answer is not None:
        try:
            ingredient = save_package_size(session, ingredient, name, answer[0], answer[1])
        except (UnitConversionError, InputUnitError) as err:
            raise NeedsPackage(pack or "package", str(err)) from err
    package_known = ingredient.package_qty if ingredient is not None else None
    package_known_unit = ingredient.package_unit if ingredient is not None else None
    resolved = resolve_entry(
        count,
        typed,
        package_qty=package_known,
        package_unit=package_known_unit,
    )

    if resolved.packages is not None:
        base = ingredient.base_unit if ingredient is not None else None
        return ingredient, resolved.packages, PACKAGE_COUNT_UNIT, None, 0.0, base
    purchase_unit = resolved.unit or "count"
    return ingredient, resolved.quantity, purchase_unit, None, resolved.quantity, purchase_unit


def add_manual_item(
    session: Session,
    shopping_list: ShoppingList,
    name: str,
    amount: float | None = None,
    unit: str | None = None,
    package_qty: str | None = None,
    package_unit: str | None = None,
) -> ShoppingListItem:
    """Add a free-text line. A catalog hit takes that ingredient's aisle.

    A package word with a known package is stored as a package count. Without
    one, ``NeedsPackage`` is raised and nothing is written unless ``package_qty``
    and ``package_unit`` answer it.
    """
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

    try:
        (
            ingredient,
            purchase_qty,
            purchase_unit,
            purchase_note,
            needed_qty,
            needed_unit,
        ) = _manual_purchase(
            session, ingredient, clean, amount, unit, package_qty, package_unit
        )
    except InputUnitError as exc:
        raise ShoppingError(str(exc)) from exc

    if ingredient is not None:
        label = ingredient.name
        aisle = ingredient.aisle or DEFAULT_AISLE

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


def ingredients_stocked_since(
    session: Session, user_id: int, start: datetime, end: datetime
) -> set[int]:
    """Ingredients bought from a shopping list between ``start`` and ``end`` inclusive.

    A receipt reviewed in that window unticks those lines so the same shop is
    not counted twice.
    """
    lower, upper = _aware(start), _aware(end)
    rows = session.scalars(
        live_events(user_id).where(
            InventoryEvent.reason == EventReason.PURCHASE,
            InventoryEvent.source_ref.like("shopping:%"),
        )
    ).all()
    return {
        event.ingredient_id
        for event in rows
        if event.occurred_at is not None and lower <= _aware(event.occurred_at) <= upper
    }


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
    needs = collect_requirements(session, entries)  # type: ignore[arg-type]
    return {ingredient_id: line.needed for ingredient_id, line in needs.items()}
