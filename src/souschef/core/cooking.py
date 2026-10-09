"""Cooking a recipe as one record, so Undo can reverse exactly what it did."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.core.inventory import consume_for_recipe, live_events, void_events
from souschef.models import Cook, InventoryEvent, MealPlanEntry, Recipe


def record_cook(
    session: Session,
    user_id: int,
    recipe: Recipe,
    used: dict[int, float],
    *,
    when: datetime,
    servings: int,
    plan_entry: MealPlanEntry | None = None,
    notes: dict[int, str] | None = None,
) -> Cook:
    """Debit the pantry for a cook, close its plan entry and count it on the recipe."""
    cook = Cook(
        user_id=user_id,
        recipe_id=recipe.id,
        plan_entry_id=plan_entry.id if plan_entry is not None else None,
        servings=servings or None,
        cooked_at=when,
    )
    session.add(cook)
    session.flush()
    consume_for_recipe(
        session,
        user_id,
        used,
        recipe_id=recipe.id,
        notes=notes or None,
        occurred_at=when,
        cook_id=cook.id,
    )

    recipe.times_cooked += 1
    previous = recipe.last_cooked_at
    if previous is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if previous is None or previous < when:
        recipe.last_cooked_at = when

    if plan_entry is not None:
        plan_entry.cooked_at = when
        if servings:
            plan_entry.servings = servings
    session.flush()
    return cook


def undo_cook(session: Session, user_id: int, cook_id: int) -> Cook | None:
    """Void a cook's events and reopen the plan entry it closed.

    ``times_cooked`` is decremented rather than recounted because a cook that
    wrote no events (an all-staples recipe) leaves nothing to count.
    """
    cook = session.get(Cook, cook_id)
    if cook is None or cook.user_id != user_id or cook.undone_at is not None:
        return None
    event_ids = list(
        session.scalars(
            live_events(user_id)
            .with_only_columns(InventoryEvent.id)
            .where(InventoryEvent.cook_id == cook.id)
        )
    )
    void_events(session, user_id, event_ids)
    cook.undone_at = datetime.now(UTC)

    if cook.plan_entry_id is not None:
        entry = session.get(MealPlanEntry, cook.plan_entry_id)
        if entry is not None:
            entry.cooked_at = None

    recipe = session.get(Recipe, cook.recipe_id)
    if recipe is not None:
        recipe.times_cooked = max(0, recipe.times_cooked - 1)
        recipe.last_cooked_at = session.scalar(
            select(func.max(Cook.cooked_at)).where(
                Cook.recipe_id == recipe.id, Cook.undone_at.is_(None)
            )
        )
    session.flush()
    return cook
