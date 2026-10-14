"""Routes: plan."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from souschef.db import get_db
from souschef.models import MealPlanEntry, MealSlot, Recipe, User
from souschef.planning.suggest import Surface, by_surface, suggest
from souschef.web import auth
from souschef.web.common import (
    MAX_SERVINGS,
    PAST_DAYS,
    PLAN_DAYS,
    _default_servings,
    _horizon,
    _owned,
    _render,
    _safe_next,
    _sync_shopping,
)

router = APIRouter()


_RECIPE_GROUPS = (
    (Surface.COOK_NOW, "Ready"),
    (Surface.NEARLY_THERE, "Missing 1–3"),
    (Surface.REACH, "Missing 4+"),
)


def _recipe_groups(
    db: Session, user_id: int, entries: list[MealPlanEntry], today: date
) -> list[tuple[str, list[tuple[Recipe, str]]]]:
    """Every recipe for the day pickers, grouped by how cookable it is now."""
    planned = _planned_days(entries, today)
    grouped = by_surface(suggest(db, user_id, limit=100_000))
    return [
        (
            label,
            [
                (s.recipe, f" · planned {planned[s.recipe.id]}" if s.recipe.id in planned else "")
                for s in grouped[surface]
            ],
        )
        for surface, label in _RECIPE_GROUPS
        if grouped[surface]
    ]


def _planned_days(entries: list[MealPlanEntry], today: date) -> dict[int, str]:
    """Upcoming days each recipe is already on, for the "planned Thu" marker."""
    planned: dict[int, list[date]] = {}
    for entry in entries:
        if (
            entry.recipe_id is not None
            and entry.plan_date >= today
            and entry.cooked_at is None
            and entry.skipped_at is None
        ):
            planned.setdefault(entry.recipe_id, []).append(entry.plan_date)
    return {
        recipe_id: ", ".join(day.strftime("%a") for day in days)
        for recipe_id, days in planned.items()
    }


@router.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    skipped: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    today, week_end = _horizon()
    past_start = today - timedelta(days=PAST_DAYS)
    entries = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= past_start,
            MealPlanEntry.plan_date <= week_end,
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()

    days = [today + timedelta(days=offset) for offset in range(PLAN_DAYS)]
    span = (week_end - past_start).days + 1
    shown = [past_start + timedelta(days=offset) for offset in range(span)]
    by_day = {day: [e for e in entries if e.plan_date == day] for day in shown}

    return _render(
        request,
        "plan.html",
        by_day=by_day,
        days=days,
        today=today,
        has_meals=any(by_day.values()),
        past_days=[day for day in shown if day < today and by_day[day]],
        recipe_groups=_recipe_groups(db, user.id, entries, today),
        week_end=week_end,
        skipped=bool(skipped),
    )


def _plan_recipe(db: Session, user_id: int, recipe_id: int, day: date, servings: int) -> bool:
    """Plan a recipe on a day. False when it is already there.

    The same recipe twice on one day doubled every ingredient on the list with
    no signal that it had happened.
    """
    already = db.scalar(
        select(MealPlanEntry.id).where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.recipe_id == recipe_id,
            MealPlanEntry.plan_date == day,
        )
    )
    if already is not None:
        return False
    db.add(
        MealPlanEntry(
            user_id=user_id,
            plan_date=day,
            slot=MealSlot.DINNER,
            recipe_id=recipe_id,
            servings=servings,
        )
    )
    return True


@router.post("/plan/add")
async def add_to_plan(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    recipe_id = int(form.get("recipe_id") or 0)
    # Validate the recipe is theirs before planning it: without this, posting an
    # arbitrary id would put another user's recipe on your calendar, and the
    # plan page would then render its title.
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    servings = min(int(form.get("servings") or 0), MAX_SERVINGS) or _default_servings(recipe)
    skipped = False
    added = False
    for raw in form.getlist("plan_date"):
        value = str(raw).strip()
        if not value:
            continue
        if _plan_recipe(db, user.id, recipe_id, date.fromisoformat(value), servings):
            added = True
        else:
            skipped = True
    if added:
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse("/plan?skipped=1" if skipped else "/plan", status_code=303)


NOTE_TITLES = ("Eating out", "Leftovers")


@router.post("/plan/day")
def plan_day(
    plan_date: date = Form(...),
    choice: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Put one thing on one day: a recipe ("r:<id>") or a night off ("n:<title>")."""
    kind, _, value = choice.partition(":")
    if kind == "r" and value.isdigit():
        recipe = _owned(db, Recipe, int(value), user, "recipe")
        if _plan_recipe(db, user.id, recipe.id, plan_date, _default_servings(recipe)):
            db.flush()
            _sync_shopping(db, user.id)
    elif kind == "n" and value in NOTE_TITLES:
        taken = db.scalar(
            select(MealPlanEntry.id).where(
                MealPlanEntry.user_id == user.id,
                MealPlanEntry.plan_date == plan_date,
                MealPlanEntry.freeform_title == value,
            )
        )
        if taken is None:
            db.add(
                MealPlanEntry(
                    user_id=user.id,
                    plan_date=plan_date,
                    slot=MealSlot.DINNER,
                    freeform_title=value,
                )
            )
    return RedirectResponse(f"/plan#d-{plan_date.isoformat()}", status_code=303)


@router.post("/plan/{entry_id}/servings")
def set_plan_servings(
    entry_id: int,
    servings: int = Form(..., ge=1, le=MAX_SERVINGS),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
    if entry.recipe is None or entry.cooked_at is not None or entry.skipped_at is not None:
        raise HTTPException(status_code=404, detail="No such plan entry")
    entry.servings = servings
    entry.recipe.last_servings = servings
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/plan#d-{entry.plan_date.isoformat()}", status_code=303)


@router.post("/plan/{entry_id}/remove")
def remove_from_plan(
    entry_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    entry = db.get(MealPlanEntry, entry_id)
    if entry is not None and entry.user_id == user.id:
        db.delete(entry)
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse("/plan", status_code=303)


@router.post("/plan/{entry_id}/skip")
def skip_meal(
    entry_id: int,
    next_path: str = Form("/", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Keep the plan entry and debit nothing. The meal didn't happen."""
    entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
    if entry.cooked_at is None and entry.skipped_at is None:
        entry.skipped_at = datetime.now(UTC)
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse(_safe_next(next_path), status_code=303)
