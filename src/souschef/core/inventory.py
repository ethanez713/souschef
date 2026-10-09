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

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from souschef.config import Settings, get_settings
from souschef.core.belief import OBSERVATIONS, Belief, BeliefEvent, aware, drift, step
from souschef.core.units import (
    IngredientPhysical,
    UnitConversionError,
    count_factors_for,
    to_base,
)
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    PantryItem,
    StorageLocation,
    TrackingMode,
)

# A receipt line with no matcher score is stored at 0.5 (ingest/receipt_service).
# A purchase we inferred — a ticked shopping line, or cooking more than the
# ledger held — is a weaker claim than a scan, so it stays under that floor.
INFERRED_PURCHASE_CONFIDENCE = 0.35


def physical_of(ingredient: CanonicalIngredient) -> IngredientPhysical:
    return IngredientPhysical(
        density_g_per_ml=ingredient.density_g_per_ml,
        grams_per_count=ingredient.grams_per_count,
        base_unit=ingredient.base_unit,
        count_factors=count_factors_for(ingredient.name) or None,
    )


def live_events(user_id: int) -> Select[tuple[InventoryEvent]]:
    """Every ledger read goes through here, so a voided event is never counted."""
    return select(InventoryEvent).where(
        InventoryEvent.user_id == user_id, InventoryEvent.voided_at.is_(None)
    )


def half_life_days(mode: TrackingMode, settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    if mode == TrackingMode.DISCRETE:
        return settings.confidence_half_life_discrete_days
    return settings.confidence_half_life_bulk_days


@dataclass(frozen=True)
class PantryView:
    """A pantry row with drift applied. What the UI and planner actually read."""

    item: PantryItem
    ingredient: CanonicalIngredient
    quantity: float
    low: float
    high: float
    is_known: bool

    @property
    def certainty(self) -> float:
        return self.low / self.high if self.high > 0 else 1.0

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
            low=0.0,
            high=0.0,
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


def _belief_of(item: PantryItem) -> Belief:
    last_confirmed = item.last_confirmed_at if item.belief_at is not None else None
    return Belief(item.quantity, item.low, item.high, item.belief_at, last_confirmed)


def _store(item: PantryItem, belief: Belief) -> None:
    item.quantity, item.low, item.high = belief.quantity, belief.low, belief.high
    item.belief_at = belief.belief_at
    if belief.last_confirmed_at is not None:
        item.last_confirmed_at = belief.last_confirmed_at


def _fold_step(
    belief: Belief, event: InventoryEvent, ingredient: CanonicalIngredient
) -> Belief:
    settings = get_settings()
    folded = step(
        belief,
        BeliefEvent(
            event.reason,
            event.delta,
            event.resulting_quantity,
            event.uncertainty or 0.0,
            event.occurred_at,
        ),
        half_life_days=half_life_days(ingredient.tracking_mode, settings),
        cook_estimate_error=settings.cook_estimate_error,
    )
    if event.reason not in OBSERVATIONS:
        event.resulting_quantity = folded.quantity
    return folded


def _refold(session: Session, item: PantryItem, ingredient: CanonicalIngredient) -> None:
    """Recompute one item from its live events, in (occurred_at, id) order."""
    events = session.scalars(
        live_events(item.user_id).where(InventoryEvent.ingredient_id == ingredient.id)
    ).all()
    belief = Belief(last_confirmed_at=None)
    for event in sorted(events, key=lambda e: (aware(e.occurred_at), e.id)):
        belief = _fold_step(belief, event, ingredient)
    _store(item, belief)


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
    level: float | None = None,
    uncertainty: float = 0.0,
    cook_id: int | None = None,
) -> InventoryEvent:
    """Append a ledger entry and fold it into the pantry row.

    ``delta`` is interpreted in ``unit`` (defaulting to the ingredient's base
    unit) and converted before storage, so callers can speak in whatever units
    their source used. An observation (CONFIRM, MANUAL, INITIAL) records the
    absolute ``level`` the user saw; without one it is the current level plus delta.
    """
    physical = physical_of(ingredient)
    # Convert before creating the pantry row. A unit we can't convert used to
    # leave a zero row behind, because the row was inserted first and the
    # request then caught the error and committed.
    if unit is not None and delta:
        delta_base = to_base(abs(delta), unit, physical)
        delta_base = -delta_base if delta < 0 else delta_base
    else:
        delta_base = delta

    item = get_or_create_item(session, user_id, ingredient)
    if reason in OBSERVATIONS:
        resulting = level if level is not None else max(0.0, item.quantity + delta_base)
    else:
        resulting = max(0.0, item.quantity + delta_base)

    event = InventoryEvent(
        user_id=user_id,
        ingredient_id=ingredient.id,
        reason=reason,
        delta=delta_base,
        resulting_quantity=resulting,
        uncertainty=uncertainty,
        event_confidence=event_confidence,
        source_ref=source_ref,
        note=note,
        occurred_at=occurred_at or datetime.now(UTC),
        cook_id=cook_id,
    )
    session.add(event)
    session.flush()

    if item.belief_at is None or aware(event.occurred_at) >= aware(item.belief_at):
        _store(item, _fold_step(_belief_of(item), event, ingredient))
    else:
        _refold(session, item, ingredient)

    if expires_on is not None:
        item.expires_on = expires_on

    session.flush()
    return event


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
    if quantity < 0:
        raise ValueError("Amount has to be zero or more.")
    target = (
        to_base(quantity, unit, physical_of(ingredient)) if unit is not None else quantity
    )
    item = get_or_create_item(session, user_id, ingredient)
    return record_event(
        session,
        user_id,
        ingredient,
        reason=EventReason.MANUAL,
        delta=target - item.quantity,
        level=target,
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
    existing estimate was right. Either way the range collapses -- which is the
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


def discard_item(
    session: Session, user_id: int, ingredient: CanonicalIngredient
) -> InventoryEvent | None:
    """Throw out everything held of an ingredient. None when there was nothing."""
    item = get_or_create_item(session, user_id, ingredient)
    if item.quantity <= 0:
        return None
    return record_event(
        session,
        user_id,
        ingredient,
        reason=EventReason.WASTE,
        delta=-item.quantity,
        note="thrown out",
    )


def void_events(session: Session, user_id: int, event_ids: list[int]) -> list[InventoryEvent]:
    """Undo by ignoring: mark the events voided and refold what they touched."""
    now = datetime.now(UTC)
    events = [
        event
        for event in session.scalars(
            live_events(user_id).where(InventoryEvent.id.in_(event_ids))
        )
    ]
    for event in events:
        event.voided_at = now
    session.flush()
    for ingredient_id in {event.ingredient_id for event in events}:
        ingredient = session.get(CanonicalIngredient, ingredient_id)
        if ingredient is not None:
            _refold(session, get_or_create_item(session, user_id, ingredient), ingredient)
    session.flush()
    return events


def undo_discard(session: Session, user_id: int, event_id: int) -> InventoryEvent | None:
    """Put back what a discard removed by voiding it; later events replay on top."""
    waste = session.get(InventoryEvent, event_id)
    if waste is None or waste.user_id != user_id or waste.reason != EventReason.WASTE:
        return None
    voided = void_events(session, user_id, [waste.id])
    return voided[0] if voided else None


def consume_for_recipe(
    session: Session,
    user_id: int,
    requirements: dict[int, float],
    *,
    recipe_id: int,
    occurred_at: datetime | None = None,
    notes: dict[int, str] | None = None,
    cook_id: int | None = None,
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
                cook_id=cook_id,
            )
        )
    return events


def pantry_view(
    session: Session, user_id: int, *, include_empty: bool = False
) -> list[PantryView]:
    """One user's current pantry, with drift applied."""
    rows = session.execute(
        select(PantryItem, CanonicalIngredient)
        .join(CanonicalIngredient, PantryItem.ingredient_id == CanonicalIngredient.id)
        .where(PantryItem.user_id == user_id)
        .order_by(CanonicalIngredient.name)
    ).all()
    known = set(
        session.scalars(
            live_events(user_id)
            .with_only_columns(InventoryEvent.ingredient_id)
            .where(InventoryEvent.reason != EventReason.COOK)
            .distinct()
        )
    )

    now = datetime.now(UTC)
    views = []
    for item, ingredient in rows:
        if not include_empty and item.quantity <= 0:
            continue
        belief = drift(_belief_of(item), now, half_life_days(ingredient.tracking_mode))
        views.append(
            PantryView(
                item=item,
                ingredient=ingredient,
                quantity=belief.quantity,
                low=belief.low,
                high=belief.high,
                is_known=ingredient.id in known,
            )
        )
    return views


def rebuild_from_ledger(session: Session, user_id: int) -> int:
    """Recompute one user's pantry rows by replaying their ledger.

    The materialized rows are a cache; this proves it. Useful after editing
    history, and as a self-check that the fold and the incremental path agree.
    """
    ingredient_ids = {
        row
        for row in session.scalars(
            select(PantryItem.ingredient_id).where(PantryItem.user_id == user_id)
        )
    } | set(session.scalars(live_events(user_id).with_only_columns(InventoryEvent.ingredient_id)))

    updated = 0
    for ingredient_id in sorted(ingredient_ids):
        ingredient = session.get(CanonicalIngredient, ingredient_id)
        if ingredient is None:
            continue
        _refold(session, get_or_create_item(session, user_id, ingredient), ingredient)
        updated += 1

    session.flush()
    return updated
