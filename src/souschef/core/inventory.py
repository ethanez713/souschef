"""Pantry mutations.

Every change goes through :func:`record_event`, which appends to the ledger and
then updates the materialized ``PantryItem``. Nothing writes a level directly.
That single rule is what makes state reconstructible, makes "why does it think I
have three onions?" answerable, and makes undo trivial.

Every kitchen belongs to one user, so ``user_id`` is a required second argument
throughout this module. It is positional rather than an optional keyword
deliberately: a default would let a caller forget it and silently read or write
someone else's pantry, and that is the one bug in a multi-user app that must be
impossible to write rather than merely caught in review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.estimation import confidence_for_item
from souschef.core.units import IngredientPhysical, UnitConversionError, to_base
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    PantryItem,
    StorageLocation,
)


def physical_of(ingredient: CanonicalIngredient) -> IngredientPhysical:
    return IngredientPhysical(
        density_g_per_ml=ingredient.density_g_per_ml,
        grams_per_count=ingredient.grams_per_count,
        base_unit=ingredient.base_unit,
    )


@dataclass(frozen=True)
class PantryView:
    """A pantry row with decay applied. What the UI and planner actually read."""

    item: PantryItem
    ingredient: CanonicalIngredient
    quantity: float
    confidence: float

    @property
    def is_staple(self) -> bool:
        return self.ingredient.is_staple

    @property
    def is_out(self) -> bool:
        return self.quantity <= 0

    @property
    def packages_remaining(self) -> float | None:
        """Level expressed in purchasable packages, which is how people think."""
        if not self.ingredient.package_qty:
            return None
        try:
            pkg_base = to_base(
                self.ingredient.package_qty,
                self.ingredient.package_unit,
                physical_of(self.ingredient),
            )
        except UnitConversionError:
            return None
        return self.quantity / pkg_base if pkg_base else None


def get_or_create_item(
    session: Session,
    user_id: int,
    ingredient: CanonicalIngredient,
    *,
    location: StorageLocation | None = None,
) -> PantryItem:
    item = session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == user_id, PantryItem.ingredient_id == ingredient.id
        )
    )
    if item is None:
        item = PantryItem(
            user_id=user_id,
            ingredient_id=ingredient.id,
            quantity=0.0,
            confidence=1.0,
            location=location or _default_location(ingredient),
        )
        session.add(item)
        session.flush()
    return item


def _default_location(ingredient: CanonicalIngredient) -> StorageLocation:
    category = str(ingredient.category)
    if category in ("dairy", "meat", "seafood", "produce"):
        return StorageLocation.FRIDGE
    if category == "frozen":
        return StorageLocation.FREEZER
    return StorageLocation.PANTRY


def record_event(
    session: Session,
    user_id: int,
    ingredient: CanonicalIngredient,
    *,
    reason: EventReason,
    delta: float = 0.0,
    unit: str | None = None,
    event_confidence: float = 1.0,
    source_ref: str | None = None,
    note: str | None = None,
    occurred_at: datetime | None = None,
    expires_on: date | None = None,
) -> InventoryEvent:
    """Append a ledger entry and fold it into the pantry row.

    ``delta`` is interpreted in ``unit`` (defaulting to the ingredient's base
    unit) and converted before storage, so callers can speak in whatever units
    their source used.
    """
    item = get_or_create_item(session, user_id, ingredient)
    physical = physical_of(ingredient)

    if unit is not None and delta:
        delta_base = to_base(abs(delta), unit, physical)
        delta_base = -delta_base if delta < 0 else delta_base
    else:
        delta_base = delta

    new_quantity = max(0.0, item.quantity + delta_base)

    event = InventoryEvent(
        user_id=user_id,
        ingredient_id=ingredient.id,
        reason=reason,
        delta=delta_base,
        resulting_quantity=new_quantity,
        event_confidence=event_confidence,
        source_ref=source_ref,
        note=note,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(event)

    item.quantity = new_quantity
    _apply_confidence(item, reason, event_confidence)

    if expires_on is not None:
        item.expires_on = expires_on

    session.flush()
    return event


def _apply_confidence(item: PantryItem, reason: EventReason, event_confidence: float) -> None:
    """Update trust in the level according to what just happened."""
    now = datetime.now(UTC)

    if reason in (EventReason.CONFIRM, EventReason.MANUAL, EventReason.INITIAL):
        # The user physically looked. This is the only thing that fully restores
        # trust, and it resets the decay anchor.
        item.confidence = 1.0
        item.last_confirmed_at = now
        item.cooks_since_confirmed = 0
        return

    if reason == EventReason.PURCHASE:
        # A purchase tells us a lot but not everything: we know what went in,
        # not what was already there. Trust rises toward, but not to, certainty.
        item.confidence = min(1.0, max(item.confidence, 0.5) + 0.35 * event_confidence)
        return

    if reason == EventReason.COOK:
        # Subtracting an estimate. Track the count; decay does the rest.
        item.cooks_since_confirmed += 1
        return

    if reason == EventReason.WASTE:
        item.confidence = min(1.0, item.confidence + 0.2)


def set_level(
    session: Session,
    user_id: int,
    ingredient: CanonicalIngredient,
    quantity: float,
    *,
    unit: str | None = None,
    note: str | None = None,
) -> InventoryEvent:
    """Set an absolute level, as when the user corrects an estimate.

    Recorded as a MANUAL delta rather than an overwrite so the ledger still
    explains the jump.
    """
    item = get_or_create_item(session, user_id, ingredient)
    target = (
        to_base(quantity, unit, physical_of(ingredient)) if unit is not None else quantity
    )
    return record_event(
        session,
        user_id,
        ingredient,
        reason=EventReason.MANUAL,
        delta=target - item.quantity,
        note=note or "manual level correction",
    )


def confirm_level(
    session: Session,
    user_id: int,
    ingredient: CanonicalIngredient,
    quantity: float | None = None,
    *,
    unit: str | None = None,
) -> InventoryEvent:
    """Record that the user physically checked an item.

    With ``quantity`` this corrects the level; without it, it asserts the
    existing estimate was right. Either way confidence resets -- which is the
    whole point of the pre-shop audit.
    """
    if quantity is not None:
        return set_level(
            session, user_id, ingredient, quantity, unit=unit, note="confirmed by check"
        )
    return record_event(
        session,
        user_id,
        ingredient,
        reason=EventReason.CONFIRM,
        delta=0.0,
        note="confirmed unchanged",
    )


def consume_for_recipe(
    session: Session,
    user_id: int,
    requirements: dict[int, float],
    *,
    recipe_id: int,
    occurred_at: datetime | None = None,
    notes: dict[int, str] | None = None,
) -> list[InventoryEvent]:
    """Debit the pantry for a cooked recipe.

    ``requirements`` maps ingredient id to a quantity already in base units.
    Staples are skipped: they are assumed always stocked, so cooking never
    debits them. ``notes`` is per-ingredient, for when the cook preview was edited.
    """
    events: list[InventoryEvent] = []
    for ingredient_id, amount in requirements.items():
        if amount <= 0:
            continue
        ingredient = session.get(CanonicalIngredient, ingredient_id)
        if ingredient is None or ingredient.is_staple:
            continue
        events.append(
            record_event(
                session,
                user_id,
                ingredient,
                reason=EventReason.COOK,
                delta=-amount,
                source_ref=f"recipe:{recipe_id}",
                note=(notes or {}).get(ingredient_id),
                occurred_at=occurred_at,
            )
        )
    return events


def pantry_view(
    session: Session, user_id: int, *, include_empty: bool = False
) -> list[PantryView]:
    """One user's current pantry, with decay applied."""
    rows = session.execute(
        select(PantryItem, CanonicalIngredient)
        .join(CanonicalIngredient, PantryItem.ingredient_id == CanonicalIngredient.id)
        .where(PantryItem.user_id == user_id)
        .order_by(CanonicalIngredient.name)
    ).all()

    views = []
    for item, ingredient in rows:
        if not include_empty and item.quantity <= 0:
            continue
        views.append(
            PantryView(
                item=item,
                ingredient=ingredient,
                quantity=item.quantity,
                confidence=confidence_for_item(item),
            )
        )
    return views


def rebuild_from_ledger(session: Session, user_id: int) -> int:
    """Recompute one user's pantry rows by replaying their ledger.

    The materialized rows are a cache; this proves it. Useful after editing
    history, and as a self-check that the fold and the incremental path agree.
    """
    events = session.scalars(
        select(InventoryEvent)
        .where(InventoryEvent.user_id == user_id)
        .order_by(InventoryEvent.occurred_at, InventoryEvent.id)
    ).all()

    totals: dict[int, float] = {}
    last_confirmed: dict[int, datetime] = {}
    cooks: dict[int, int] = {}

    for event in events:
        current = totals.get(event.ingredient_id, 0.0)
        totals[event.ingredient_id] = max(0.0, current + event.delta)
        if event.reason in (EventReason.CONFIRM, EventReason.MANUAL, EventReason.INITIAL):
            last_confirmed[event.ingredient_id] = event.occurred_at
            cooks[event.ingredient_id] = 0
        elif event.reason == EventReason.COOK:
            cooks[event.ingredient_id] = cooks.get(event.ingredient_id, 0) + 1

    updated = 0
    for ingredient_id, quantity in totals.items():
        ingredient = session.get(CanonicalIngredient, ingredient_id)
        if ingredient is None:
            continue
        item = get_or_create_item(session, user_id, ingredient)
        item.quantity = quantity
        item.cooks_since_confirmed = cooks.get(ingredient_id, 0)
        if ingredient_id in last_confirmed:
            item.last_confirmed_at = last_confirmed[ingredient_id]
        updated += 1

    session.flush()
    return updated
