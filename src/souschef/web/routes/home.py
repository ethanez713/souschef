"""Routes: home."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from souschef.core.inventory import pantry_view
from souschef.core.spoilage import freshness_by_ingredient
from souschef.db import get_db
from souschef.models import MealPlanEntry, Recipe, User
from souschef.planning.suggest import Surface, by_surface, suggest
from souschef.web import auth
from souschef.web.common import (
    PAST_DAYS,
    _change_toast,
    _on_hand,
    _render,
)

router = APIRouter()


def render_home(
    request: Request,
    db: Session,
    user: User,
    *,
    changed: int = 0,
    form_error: str = "",
) -> HTMLResponse:
    suggestions = suggest(db, user.id, limit=12)
    grouped = by_surface(suggestions)

    today = date.today()
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
    upcoming = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= today,
            MealPlanEntry.cooked_at.is_(None),
        )
        .order_by(MealPlanEntry.plan_date)
        .limit(7)
    ).all()

    pantry = pantry_view(db, user.id)
    freshness = freshness_by_ingredient(db, user.id, pantry, today)
    stale = sorted(
        (v for v in pantry if v.certainty < 0.5 and _on_hand(v)),
        key=lambda v: v.certainty,
    )[:5]

    return _render(
        request,
        "dashboard.html",
        cook_now=grouped[Surface.COOK_NOW][:3],
        nearly=grouped[Surface.NEARLY_THERE][:3],
        bigger_shop=grouped[Surface.REACH][:3],
        past_meals=past_meals,
        upcoming=upcoming,
        today=today,
        freshness=freshness,
        stale=stale,
        change=_change_toast(db, user, changed),
        form_error=form_error,
        pantry_count=len(pantry),
        recipe_count=db.scalar(
            select(func.count())
            .select_from(Recipe)
            .where(Recipe.user_id == user.id, Recipe.is_archived.is_(False))
        )
        or 0,
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    changed: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return render_home(request, db, user, changed=changed)
