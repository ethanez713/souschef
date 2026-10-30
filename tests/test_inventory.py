"""Ledger, belief and audit tests.

These encode the behavioural promises the app is built on: state is
reconstructible from events, doubt grows for the right reasons, and the
pre-shop audit asks about few things and the *right* things.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.core.audit import AuditReason, build_audit
from souschef.core.belief import Belief, drift
from souschef.core.cooking import record_cook, undo_cook
from souschef.core.inventory import (
    confirm_level,
    consume_for_recipe,
    discard_item,
    half_life_days,
    pantry_view,
    rebuild_from_ledger,
    record_event,
    set_level,
    undo_event,
    void_events,
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


def test_bulk_doubt_grows_faster_than_discrete() -> None:
    """You know how many eggs you have; you don't know how much rice you have."""
    belief = Belief(100, 100, 100, belief_at=NOW)
    later = NOW + timedelta(days=20)
    bulk = drift(belief, later, half_life_days(TrackingMode.BULK))
    discrete = drift(belief, later, half_life_days(TrackingMode.DISCRETE))
    assert bulk.certainty < discrete.certainty


def test_the_low_bound_halves_after_one_half_life() -> None:
    settings = get_settings()
    belief = Belief(100, 100, 100, belief_at=NOW)
    later = drift(
        belief,
        NOW + timedelta(days=settings.confidence_half_life_bulk_days),
        settings.confidence_half_life_bulk_days,
    )
    assert (later.quantity, later.low, later.high) == (100, pytest.approx(50), 100)


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


def test_confirming_collapses_the_range_and_resets_the_anchor(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    consume_for_recipe(seeded_session, owner, {rice.id: 200}, recipe_id=1)
    consume_for_recipe(seeded_session, owner, {rice.id: 200}, recipe_id=2)

    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == rice.id))
    assert item.low < item.quantity < item.high

    confirm_level(seeded_session, owner, rice)
    seeded_session.refresh(item)
    assert item.low == item.quantity == item.high == pytest.approx(600)
    assert (datetime.now(UTC) - item.last_confirmed_at.replace(tzinfo=UTC)).total_seconds() < 60


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
    assert (item.quantity, item.low, item.high) == (0, 0, 0)

    rebuild_from_ledger(seeded_session, owner)
    seeded_session.refresh(item)
    assert (item.quantity, item.low, item.high) == (0, 0, 0)


def test_discarding_nothing_writes_nothing(seeded_session: Session, owner: int) -> None:
    rice = ingredient(seeded_session, "white rice")
    assert discard_item(seeded_session, owner, rice) is None
    assert seeded_session.scalars(select(InventoryEvent)).all() == []


def _row(session: Session, ing: CanonicalIngredient) -> PantryItem:
    session.expire_all()
    return session.scalar(select(PantryItem).where(PantryItem.ingredient_id == ing.id))


def _belief_fields(item: PantryItem) -> tuple:
    return (item.quantity, item.low, item.high, item.belief_at, item.last_confirmed_at)


def test_undo_voids_the_discard_and_restores_every_belief_field(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    consume_for_recipe(seeded_session, owner, {rice.id: 200}, recipe_id=1)
    before = _belief_fields(_row(seeded_session, rice))
    waste = discard_item(seeded_session, owner, rice)

    undone = undo_event(seeded_session, owner, waste.id)

    assert undone is not None and undone.voided_at is not None
    assert _belief_fields(_row(seeded_session, rice)) == before
    assert len(seeded_session.scalars(select(InventoryEvent)).all()) == 3


def test_undo_stays_valid_after_later_events_which_replay_on_top(
    seeded_session: Session, owner: int
) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    waste = discard_item(seeded_session, owner, eggs)
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=6)

    assert undo_event(seeded_session, owner, waste.id) is not None
    assert _row(seeded_session, eggs).quantity == 18


def test_a_voided_event_is_skipped_by_every_read(seeded_session: Session, owner: int) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    bought = record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=6)

    void_events(seeded_session, owner, [bought.id])

    assert _row(seeded_session, eggs).quantity == 12
    rebuild_from_ledger(seeded_session, owner)
    assert _row(seeded_session, eggs).quantity == 12


def test_undo_ignores_another_users_event(seeded_session: Session, owner: int) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    waste = discard_item(seeded_session, owner, eggs)

    assert undo_event(seeded_session, owner + 1000, waste.id) is None


def test_undo_event_voids_a_confirm_and_refuses_a_cook(
    seeded_session: Session, owner: int
) -> None:
    eggs = ingredient(seeded_session, "eggs")
    record_event(seeded_session, owner, eggs, reason=EventReason.PURCHASE, delta=12)
    checked = confirm_level(seeded_session, owner, eggs)

    undone = undo_event(seeded_session, owner, checked.id)

    assert undone is not None and undone.voided_at is not None
    assert _row(seeded_session, eggs).quantity == 12
    cooked = record_event(seeded_session, owner, eggs, reason=EventReason.COOK, delta=-2)
    assert undo_event(seeded_session, owner, cooked.id) is None
    assert cooked.voided_at is None
    assert _row(seeded_session, eggs).quantity == 10


def _age_item(session: Session, ing: CanonicalIngredient, days: int) -> PantryItem:
    """Leave an item unlooked-at for ``days``: its low bound drifts down."""
    item = session.scalar(select(PantryItem).where(PantryItem.ingredient_id == ing.id))
    item.belief_at = datetime.now(UTC) - timedelta(days=days)
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
    _age_item(seeded_session, rice, days=20)

    # Believed 5 kg, need 100 g: even the pessimistic bound still covers it.
    assert build_audit(seeded_session, owner, {rice.id: 100}) == []


def test_audit_surfaces_levels_straddling_the_requirement(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=500)
    _age_item(seeded_session, rice, days=90)

    result = build_audit(seeded_session, owner, {rice.id: 480})
    assert [c.ingredient.name for c in result] == ["white rice"]
    candidate = result[0]
    assert candidate.straddles_decision
    assert candidate.reason == AuditReason.NEEDED_THIS_WEEK
    assert candidate.low < candidate.threshold < candidate.high


def test_audit_respects_the_item_cap(seeded_session: Session, owner: int) -> None:
    """A checklist long enough to ignore has failed at its job."""
    names = [
        "white rice",
        "all-purpose flour",
        "granulated sugar",
        "rolled oats",
        "penne",
        "quinoa",
        "brown rice",
        "couscous",
        "dried lentils",
        "breadcrumbs",
        "walnuts",
        "almonds",
    ]
    requirements = {}
    for name in names:
        ing = ingredient(seeded_session, name)
        record_event(seeded_session, owner, ing, reason=EventReason.PURCHASE, delta=500)
        _age_item(seeded_session, ing, days=120)
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
        item = _age_item(seeded_session, ing, days=90)
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
    item = _age_item(seeded_session, rice, days=90)
    item.restock_threshold = 480
    seeded_session.flush()

    result = build_audit(seeded_session, owner, {})
    assert [c.ingredient.name for c in result] == ["white rice"]
    assert result[0].reason == AuditReason.NEAR_RESTOCK


def _fields(item: PantryItem) -> tuple[float, float, float, datetime | None, datetime]:
    return (item.quantity, item.low, item.high, item.belief_at, item.last_confirmed_at)


def test_a_random_out_of_order_history_folds_the_same_either_way(
    seeded_session: Session, owner: int
) -> None:
    rng = random.Random(20261008)
    names = ["eggs", "white rice", "all-purpose flour", "penne", "chicken breast"]
    ingredients = [ingredient(seeded_session, name) for name in names]
    start = datetime.now(UTC) - timedelta(days=60)

    for _ in range(200):
        ing = rng.choice(ingredients)
        when = start + timedelta(minutes=rng.randrange(60 * 24 * 60))
        kind = rng.choice(["purchase", "purchase", "cook", "cook", "waste", "confirm", "manual"])
        if kind == "purchase":
            record_event(
                seeded_session,
                owner,
                ing,
                reason=EventReason.PURCHASE,
                delta=rng.randrange(1, 800),
                occurred_at=when,
            )
        elif kind == "cook":
            record_event(
                seeded_session,
                owner,
                ing,
                reason=EventReason.COOK,
                delta=-rng.randrange(1, 400),
                occurred_at=when,
            )
        elif kind == "waste":
            record_event(
                seeded_session,
                owner,
                ing,
                reason=EventReason.WASTE,
                delta=-rng.randrange(1, 300),
                occurred_at=when,
            )
        elif kind == "confirm":
            record_event(seeded_session, owner, ing, reason=EventReason.CONFIRM, occurred_at=when)
        else:
            record_event(
                seeded_session,
                owner,
                ing,
                reason=EventReason.MANUAL,
                delta=0,
                level=rng.randrange(0, 900),
                uncertainty=rng.randrange(0, 50),
                occurred_at=when,
            )

    incremental = {ing.id: _fields(_row(seeded_session, ing)) for ing in ingredients}
    results = [(e.id, e.resulting_quantity) for e in seeded_session.scalars(select(InventoryEvent))]
    rebuild_from_ledger(seeded_session, owner)
    seeded_session.expire_all()

    for ing in ingredients:
        folded = _fields(_row(seeded_session, ing))
        assert folded[:3] == pytest.approx(incremental[ing.id][:3])
        assert folded[3:] == incremental[ing.id][3:]
    for event_id, resulting in results:
        assert seeded_session.get(InventoryEvent, event_id).resulting_quantity == pytest.approx(
            resulting
        )


def test_an_item_is_known_only_after_something_other_than_a_cook(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.COOK, delta=-100)
    view = next(
        v
        for v in pantry_view(seeded_session, owner, include_empty=True)
        if v.ingredient.id == rice.id
    )
    assert not view.is_known
    assert view.certainty == 0

    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=500)
    view = next(v for v in pantry_view(seeded_session, owner) if v.ingredient.id == rice.id)
    assert view.is_known


def _recipe_and_entry(session: Session, owner: int):  # noqa: ANN202
    from datetime import date

    from souschef.models import MealPlanEntry, MealSlot, Recipe, RecipeSource

    recipe = Recipe(user_id=owner, title="Rice", source=RecipeSource.MANUAL, servings=2)
    session.add(recipe)
    session.flush()
    entry = MealPlanEntry(
        user_id=owner, plan_date=date.today(), slot=MealSlot.DINNER, recipe_id=recipe.id, servings=2
    )
    session.add(entry)
    session.flush()
    return recipe, entry


def test_undoing_a_cook_restores_beliefs_counts_and_the_plan_entry(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    recipe, entry = _recipe_and_entry(seeded_session, owner)
    before = _fields(_row(seeded_session, rice))

    cook = record_cook(
        seeded_session,
        owner,
        recipe,
        {rice.id: 300},
        when=datetime.now(UTC),
        servings=2,
        plan_entry=entry,
    )
    assert (recipe.times_cooked, entry.cooked_at is not None) == (1, True)
    assert _row(seeded_session, rice).quantity == 700

    assert undo_cook(seeded_session, owner, cook.id) is not None

    assert _fields(_row(seeded_session, rice)) == before
    assert recipe.times_cooked == 0
    assert recipe.last_cooked_at is None
    assert entry.cooked_at is None
    assert undo_cook(seeded_session, owner, cook.id) is None


def test_undoing_a_cook_keeps_a_level_confirmed_after_it(
    seeded_session: Session, owner: int
) -> None:
    rice = ingredient(seeded_session, "white rice")
    record_event(seeded_session, owner, rice, reason=EventReason.PURCHASE, delta=1000)
    recipe, _entry = _recipe_and_entry(seeded_session, owner)
    cook = record_cook(
        seeded_session, owner, recipe, {rice.id: 300}, when=datetime.now(UTC), servings=2
    )
    set_level(seeded_session, owner, rice, 650)

    undo_cook(seeded_session, owner, cook.id)

    item = _row(seeded_session, rice)
    assert (item.quantity, item.low, item.high) == (650, 650, 650)
