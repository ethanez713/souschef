"""Routes: home."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from souschef.core.inventory import pantry_view
from souschef.db import get_db
from souschef.models import MealPlanEntry, MealSlot, Recipe, User
from souschef.planning.next_step import NextStepState, next_step, readiness_line
from souschef.planning.suggest import (
    Suggestion,
    Surface,
    probably_gone_ids,
    score_recipe,
    suggest,
)
from souschef.web import auth
from souschef.web.common import PAST_DAYS, _change_toast, _horizon, _render
from souschef.web.routes.shopping import shop_counts

router = APIRouter()


def _open_dinner(entries: list[MealPlanEntry]) -> MealPlanEntry | None:
    dinners = [entry for entry in entries if entry.slot == MealSlot.DINNER]
    pool = dinners or entries
    for entry in pool:
        if entry.recipe_id is not None:
            return entry
    return pool[0] if pool else None


def _meal_title(entry: MealPlanEntry) -> str:
    if entry.recipe is not None:
        return entry.recipe.title
    return entry.freeform_title or "Planned"


def _score_planned(
    db: Session, user_id: int, recipe: Recipe, servings: int, today: date
) -> Suggestion:
    views = pantry_view(db, user_id)
    gone = probably_gone_ids(db, user_id, views, today)
    pantry = {view.ingredient.id: view for view in views if view.ingredient.id not in gone}
    return score_recipe(db, recipe, pantry, today=today, servings=servings)


def render_home(
    request: Request,
    db: Session,
    user: User,
    *,
    changed: int = 0,
    form_error: str = "",
) -> HTMLResponse:
    today, horizon_end = _horizon()
    past_start = today - timedelta(days=PAST_DAYS)
    past_meals = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= past_start,
            MealPlanEntry.plan_date < today,
            MealPlanEntry.cooked_at.is_(None),
            MealPlanEntry.skipped_at.is_(None),
            MealPlanEntry.recipe_id.is_not(None),
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()
    tomorrow = today + timedelta(days=1)
    open_entries = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe).selectinload(Recipe.ingredients))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date.in_((today, tomorrow)),
            MealPlanEntry.cooked_at.is_(None),
            MealPlanEntry.skipped_at.is_(None),
        )
        .order_by(MealPlanEntry.plan_date, MealPlanEntry.id)
    ).all()
    tonight = _open_dinner([entry for entry in open_entries if entry.plan_date == today])
    tomorrow_entry = _open_dinner(
        [entry for entry in open_entries if entry.plan_date == tomorrow]
    )

    recipe_count = (
        db.scalar(
            select(func.count())
            .select_from(Recipe)
            .where(Recipe.user_id == user.id, Recipe.is_archived.is_(False))
        )
        or 0
    )
    planned_ahead = (
        db.scalar(
            select(func.count())
            .select_from(MealPlanEntry)
            .where(
                MealPlanEntry.user_id == user.id,
                MealPlanEntry.plan_date >= today,
                MealPlanEntry.plan_date <= horizon_end,
                MealPlanEntry.cooked_at.is_(None),
                MealPlanEntry.skipped_at.is_(None),
            )
        )
        or 0
    )
    counts = shop_counts(db, user.id)
    step = next_step(
        NextStepState(
            recipe_count=recipe_count,
            planned_ahead=planned_ahead,
            check_count=counts.check_count,
            to_get=counts.to_get,
            in_basket=counts.in_basket,
        )
    )

    tonight_readiness = None
    offer = None
    offer_readiness = None
    if tonight is not None and tonight.recipe is not None:
        scored = _score_planned(db, user.id, tonight.recipe, tonight.servings, today)
        names = [gap.ingredient.name for gap in scored.missing]
        tonight_readiness = readiness_line(names, scored.unidentified)
    elif tonight is None:
        ranked = suggest(db, user.id, limit=1, today=today)
        if ranked and ranked[0].surface in (Surface.COOK_NOW, Surface.NEARLY_THERE):
            offer = ranked[0]
            names = [gap.ingredient.name for gap in offer.missing]
            offer_readiness = readiness_line(names, offer.unidentified)

    return _render(
        request,
        "dashboard.html",
        past_meals=past_meals,
        today=today,
        tonight=tonight,
        tonight_readiness=tonight_readiness,
        offer=offer,
        offer_readiness=offer_readiness,
        step=step,
        tomorrow_title=_meal_title(tomorrow_entry) if tomorrow_entry is not None else "",
        change=_change_toast(db, user, changed),
        form_error=form_error,
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    changed: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return render_home(request, db, user, changed=changed)
