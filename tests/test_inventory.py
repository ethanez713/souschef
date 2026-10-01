"""Ledger, decay and audit tests.

These encode the behavioural promises the app is built on: state is
reconstructible from events, confidence erodes for the right reasons, and the
pre-shop audit asks about few things and the *right* things.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.core.audit import AuditReason, build_audit
from souschef.core.estimation import (
    DecayInputs,
    current_confidence,
    days_until_confidence,
    estimated_bounds,
)
from souschef.core.inventory import (
    confirm_level,
    consume_for_recipe,
    discard_item,
    pantry_view,
    rebuild_from_ledger,
    record_event,
    set_level,
    undo_discard,
)
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    PantryItem,
    TrackingMode,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def ingredient(session: Session, name: str) -> CanonicalIngredient:
    ing = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert ing is not None, f"missing seed ingredient {name!r}"
    return ing


def test_confidence_decays_with_time() -> None:
    settings = get_settings()
    fresh = current_confidence(
        DecayInputs(TrackingMode.BULK, NOW, 0), now=NOW, settings=settings
    )
    later = current_confidence(
        DecayInputs(TrackingMode.BULK, NOW, 0),
        now=NOW + timedelta(days=settings.confidence_half_life_bulk_days),
        settings=settings,
    )
    assert fresh == pytest.approx(1.0)
    assert later == pytest.approx(0.5, abs=0.01)


def test_bulk_decays_faster_than_discrete() -> None:
    """You know how many eggs you have; you don't know how much rice you have."""
    after = NOW + timedelta(days=20)
    bulk = current_confidence(DecayInputs(TrackingMode.BULK, NOW, 0), now=after)
    discrete = current_confidence(DecayInputs(TrackingMode.DISCRETE, NOW, 0), now=after)
    assert bulk < discrete


def test_cooking_costs_confidence_beyond_elapsed_time() -> None:
    """Each cook subtracts an estimate, so error compounds per event."""
    no_cooks = current_confidence(DecayInputs(TrackingMode.BULK, NOW, 0), now=NOW)
    three_cooks = current_confidence(DecayInputs(TrackingMode.BULK, NOW, 3), now=NOW)
    assert three_cooks < no_cooks
    assert three_cooks == pytest.approx(
        1.0 - 3 * get_settings().confidence_penalty_per_cook, abs=1e-6
    )


def test_confidence_is_clamped() -> None:
    ancient = current_confidence(
        DecayInputs(TrackingMode.BULK, NOW, 50), now=NOW + timedelta(days=3650)
    )
    assert 0.0 < ancient <= 1.0
    # Never exceeds certainty even with a future anchor.
    assert current_confidence(
        DecayInputs(TrackingMode.BULK, NOW, 0), now=NOW - timedelta(days=5)
    ) <= 1.0


def test_days_until_confidence_matches_the_decay_curve() -> None:
    days = days_until_confidence(TrackingMode.BULK, 0.5)
    assert days == pytest.approx(get_settings().confidence_half_life_bulk_days, abs=0.01)


def test_bounds_widen_as_confidence_falls() -> None:
    certain_low, certain_high = estimated_bounds(100, 1.0)
    assert (certain_low, certain_high) == (100, 100)

    vague_low, vague_high = estimated_bounds(100, 0.2)
    assert vague_low < 100 < vague_high
    assert vague_low >= 0


def test_every_change_writes_a_ledger_entry(seeded_session: Session, owner: int) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    record_event(seeded_session, owner, eggs, reason=EventReason.COOK, delta=-3)

    events = seeded_session.scalars(
        select(InventoryEvent).where(InventoryEvent.ingredient_id == eggs.id)
    ).all()
    assert [e.reason for e in events] == [EventReason.PURCHASE, EventReason.COOK]
    assert [e.resulting_quantity for e in events] == [12, 9]


def test_quantity_never_goes_negative(seeded_session: Session, owner: int) -> None:
    """A recipe wanting more than you have empties the jar, it doesn't invert it."""
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=2)
    record_event(seeded_session, owner, eggs, reason=EventReason.COOK, delta=-10)

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == eggs.id))
    assert item.quantity == 0


def test_purchase_units_are_converted_to_base(seeded_session: Session, owner: int) -> None:
    """A 5 lb bag of flour must land as grams, not as the number 5."""
    flour = ingredient(seeded_session, "all-purpose flour")
    record_event(seeded_session, owner, flour, reason=EventReason.PURCHASE, delta=5, unit="lb")

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == flour.id))
    assert item.quantity == pytest.approx(2268, rel=0.01)


def test_confirming_restores_confidence_and_resets_the_anchor(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    consume_for_recipe(seeded_session, owner, {rice.id: 200}, recipe_id=1)
    consume_for_recipe(seeded_session, owner, {rice.id: 200}, recipe_id=2)

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == rice.id))
    assert item.cooks_since_confirmed == 2

    confirm_level(seeded_session, owner, rice)
    seeded_session.refresh(item)
    assert item.cooks_since_confirmed == 0
    assert item.confidence == 1.0


def test_set_level_records_the_correction_as_a_delta(seeded_session: Session, owner: int) -> None:
    """The ledger should explain a jump, not silently absorb it."""
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    set_level(seeded_session, owner, rice, 250)

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == rice.id))
    assert item.quantity == 250

    last = seeded_session.scalars(
        select(InventoryEvent)
        .where(InventoryEvent.ingredient_id == rice.id)
        .order_by(InventoryEvent.id.desc())
    ).first()
    assert last.reason == EventReason.MANUAL
    assert last.delta == pytest.approx(-750)


def test_rebuild_from_ledger_reproduces_incremental_state(
    seeded_session: Session, owner: int
) -> None:
    """The materialized row is a cache; replaying events must agree with it."""
    eggs = ingredient(seeded_session, "eggs")
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    consume_for_recipe(seeded_session, owner, {eggs.id: 2, rice.id: 150}, recipe_id=7)
    seeded_session.flush()

    before = {v.ingredient.name: v.quantity for v in pantry_view(seeded_session, owner)}
    rebuild_from_ledger(seeded_session, owner)
    after = {v.ingredient.name: v.quantity for v in pantry_view(seeded_session, owner)}

    assert before == after
    assert after["eggs"] == 10


def test_discarding_empties_the_item_and_counts_as_a_certain_zero(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    consume_for_recipe(seeded_session, owner, {rice.id: 200}, recipe_id=1)

    event = discard_item(seeded_session, owner, rice)

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == rice.id))
    assert event is not None and event.reason == EventReason.WASTE
    assert event.delta == pytest.approx(-800)
    assert item.quantity == 0
    assert item.confidence == 1.0
    assert item.cooks_since_confirmed == 0

    rebuild_from_ledger(seeded_session, owner)
    seeded_session.refresh(item)
    assert item.quantity == 0
    assert item.cooks_since_confirmed == 0


def test_discarding_nothing_writes_nothing(seeded_session: Session, owner: int) -> None:
    rice = ingredient(seeded_session, "white rice")
    assert discard_item(seeded_session, owner, rice) is None
    assert seeded_session.scalars(select(InventoryEvent)).all() == []


def test_undo_restores_the_level_through_a_correcting_event(
    seeded_session: Session, owner: int
) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    waste = discard_item(seeded_session, owner, eggs)

    undone = undo_discard(seeded_session, owner, waste.id)

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == eggs.id))
    assert undone is not None and undone.reason == EventReason.MANUAL
    assert item.quantity == 12
    assert len(seeded_session.scalars(select(InventoryEvent)).all()) == 3


def test_undo_does_nothing_once_newer_stock_is_logged(
    seeded_session: Session, owner: int
) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    waste = discard_item(seeded_session, owner, eggs)
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=6)

    assert undo_discard(seeded_session, owner, waste.id) is None
    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == eggs.id))
    assert item.quantity == 6


def test_undo_ignores_another_users_event(seeded_session: Session, owner: int) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    waste = discard_item(seeded_session, owner, eggs)

    assert undo_discard(seeded_session, owner + 1000, waste.id) is None


def _age_item(session: Session, ing: CanonicalIngredient, days: int, cooks: int = 0) -> PantryItem:
    item = session.scalar(select(PantryItem).where(PantryItem.ingredient_id == ing.id))
    item.last_confirmed_at = datetime.now(UTC) - timedelta(days=days)
    item.cooks_since_confirmed = cooks
    session.flush()
    return item


def test_audit_stays_silent_about_confident_levels(seeded_session: Session, owner: int) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    confirm_level(seeded_session, owner, rice)

    assert build_audit(seeded_session, owner, {rice.id: 500}) == []


def test_audit_ignores_items_the_plan_does_not_need(seeded_session: Session, owner: int) -> None:
    """Uncertainty about something you aren't cooking with is free."""
    rice = ingredient(seeded_session, "white rice")
    flour = ingredient(seeded_session, "all-purpose flour")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    record_event(seeded_session, owner, flour, reason=EventReason.PURCHASE, delta=1000)
    _age_item(seeded_session, rice, days=120)
    _age_item(seeded_session, flour, days=120)

    result = build_audit(seeded_session, owner, {rice.id: 900})
    assert [c.ingredient.name for c in result] == ["white rice"]


def test_audit_skips_levels_that_are_not_close_to_the_decision(
    seeded_session: Session, owner: int
) -> None:
    """Plenty, or nowhere near enough -- either way, checking changes nothing."""
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=5000)
    _age_item(seeded_session, rice, days=120)

    # Believed 5 kg, need 100 g: even the pessimistic bound covers it.
    assert build_audit(seeded_session, owner, {rice.id: 100}) == []


def test_audit_surfaces_levels_straddling_the_requirement(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=500)
    _age_item(seeded_session, rice, days=90, cooks=3)

    result = build_audit(seeded_session, owner, {rice.id: 480})
    assert [c.ingredient.name for c in result] == ["white rice"]
    candidate = result[0]
    assert candidate.straddles_decision
    assert candidate.reason == AuditReason.NEEDED_THIS_WEEK
    assert candidate.low < candidate.threshold < candidate.high


def test_audit_respects_the_item_cap(seeded_session: Session, owner: int) -> None:
    """A checklist long enough to ignore has failed at its job."""
    names = [
        "white rice", "all-purpose flour", "granulated sugar", "rolled oats",
        "penne", "quinoa", "brown rice", "couscous", "dried lentils",
        "breadcrumbs", "walnuts", "almonds",
    ]
    requirements = {}
    for name in names:
        ing = ingredient(seeded_session, name)
        record_event(seeded_session, owner, ing, reason=EventReason.PURCHASE, delta=500)
        _age_item(seeded_session, ing, days=120, cooks=4)
        requirements[ing.id] = 480

    result = build_audit(seeded_session, owner, requirements, limit=5)
    assert len(result) == 5
    # And it returns the highest-value checks, in order.
    assert result == sorted(result, key=lambda c: c.score, reverse=True)


def test_audit_never_lists_a_staple(seeded_session: Session, owner: int) -> None:
    oil = ingredient(seeded_session, "olive oil")
    chicken = ingredient(seeded_session, "chicken breast")
    assert oil.is_staple and not chicken.is_staple

    for ing in (oil, chicken):
        record_event(seeded_session, owner, ing, reason=EventReason.PURCHASE, delta=500)
        item = _age_item(seeded_session, ing, days=90, cooks=3)
        item.restock_threshold = 480
    seeded_session.flush()

    result = build_audit(seeded_session, owner, {oil.id: 480, chicken.id: 480})
    assert [c.ingredient.name for c in result] == ["chicken breast"]

    restock_only = build_audit(seeded_session, owner, {})
    assert oil.name not in [c.ingredient.name for c in restock_only]


def test_cooking_skips_staples(seeded_session: Session, owner: int) -> None:
    oil = ingredient(seeded_session, "olive oil")
    rice = ingredient(seeded_session, "white rice")
    for ing in (oil, rice):
        record_event(seeded_session, owner, ing, reason=EventReason.PURCHASE, delta=500)

    events = consume_for_recipe(seeded_session, owner, {oil.id: 100, rice.id: 100}, recipe_id=1)

    assert [e.ingredient_id for e in events] == [rice.id]
    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == oil.id))
    assert item.quantity == 500


def test_restock_candidates_can_reach_the_audit(seeded_session: Session, owner: int) -> None:
    """Regression: restock checks have a threshold but no plan requirement."""
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=500)
    item = _age_item(seeded_session, rice, days=90, cooks=3)
    item.restock_threshold = 480
    seeded_session.flush()

    result = build_audit(seeded_session, owner, {})
    assert [c.ingredient.name for c in result] == ["white rice"]
    assert result[0].reason == AuditReason.NEAR_RESTOCK
