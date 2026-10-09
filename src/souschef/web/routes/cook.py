"""Routes: cook."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import consume_for_recipe, cover_cook_shortfall, pantry_view
from souschef.core.recipes import recipe_requirements
from souschef.db import get_db
from souschef.models import MealPlanEntry, Recipe, User
from souschef.web import auth
from souschef.web.common import (
    MAX_SERVINGS,
    PAST_DAYS,
    _default_servings,
    _owned,
    _render,
    _sync_shopping,
)

router = APIRouter()


def _cook_dates(today: date | None = None) -> list[date]:
    """Today, then back through the days a missed cook can still be logged."""
    today = today or date.today()
    return [today - timedelta(days=offset) for offset in range(PAST_DAYS + 1)]


def _parse_cook_date(raw: str, today: date | None = None) -> date | None:
    text = raw.strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    if day not in set(_cook_dates(today)):
        return None
    return day


def _cook_when(cooked_on: date) -> datetime:
    if cooked_on >= date.today():
        return datetime.now(UTC)
    noon = datetime.min.time().replace(hour=12)
    return datetime.combine(cooked_on, noon, tzinfo=UTC)


def _open_plan_entry(db: Session, user_id: int, recipe_id: int, day: date) -> MealPlanEntry | None:
    """That day's still-open plan entry for this recipe, if the cook should close it."""
    return db.scalar(
        select(MealPlanEntry)
        .where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.recipe_id == recipe_id,
            MealPlanEntry.plan_date == day,
            MealPlanEntry.cooked_at.is_(None),
            MealPlanEntry.skipped_at.is_(None),
        )
        .order_by(MealPlanEntry.id)
    )


def _cook_page(
    request: Request,
    db: Session,
    user: User,
    recipe: Recipe,
    *,
    plan_entry: MealPlanEntry | None = None,
    servings: int = 0,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    servings = servings or (plan_entry.servings if plan_entry else 0) or _default_servings(recipe)
    servings = min(servings, MAX_SERVINGS)
    requirements = recipe_requirements(db, recipe, servings=servings)
    pantry = {v.ingredient.id: v for v in pantry_view(db, user.id)}

    debits: list[dict] = []
    by_id: dict[int, dict] = {}
    for req in requirements.requirements:
        if req.quantity <= 0 or req.is_staple:
            continue
        row = by_id.get(req.ingredient.id)
        if row is None:
            have = pantry[req.ingredient.id].quantity if req.ingredient.id in pantry else 0.0
            row = {"ingredient": req.ingredient, "quantity": 0.0, "have": have}
            by_id[req.ingredient.id] = row
            debits.append(row)
        row["quantity"] += req.quantity
    for row in debits:
        row["over"] = row["quantity"] > row["have"] + 1e-6

    today = date.today()
    cooked_on = today
    if plan_entry is not None and plan_entry.plan_date in set(_cook_dates(today)):
        cooked_on = plan_entry.plan_date
    return _render(
        request,
        "cook_preview.html",
        status_code=status_code,
        recipe=recipe,
        debits=debits,
        not_tracked=list(requirements.unmatched),
        servings=servings,
        entry=plan_entry,
        cook_dates=_cook_dates(today),
        cooked_on=cooked_on,
        today=today,
        error=error,
    )


@router.get("/recipes/{recipe_id}/cook", response_class=HTMLResponse)
def cook_preview(
    request: Request,
    recipe_id: int,
    entry: int = 0,
    servings: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    plan_entry = None
    if entry:
        plan_entry = _owned(db, MealPlanEntry, entry, user, "plan entry")
        if plan_entry.recipe_id != recipe.id:
            raise HTTPException(status_code=404, detail="No such plan entry")
    return _cook_page(request, db, user, recipe, plan_entry=plan_entry, servings=servings)


@router.post("/recipes/{recipe_id}/cooked")
async def mark_cooked(
    request: Request,
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    form = await request.form()
    servings = min(int(form.get("servings") or 0), MAX_SERVINGS)
    entry_id = int(form.get("entry_id") or 0)
    raw_date = str(form.get("cooked_on") or "")
    cooked_on = _parse_cook_date(raw_date)
    plan_entry = None
    if entry_id:
        plan_entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
        if plan_entry.recipe_id != recipe.id:
            raise HTTPException(status_code=404, detail="No such plan entry")
    if raw_date.strip() and cooked_on is None:
        return _cook_page(
            request,
            db,
            user,
            recipe,
            plan_entry=plan_entry,
            servings=servings,
            error="Pick a day from today back to a week ago.",
            status_code=400,
        )
    if cooked_on is None:
        cooked_on = date.today()

    proposed: dict[int, float] = {}
    used: dict[int, float] = {}
    for key, raw in form.items():
        value = str(raw).strip()
        if not value:
            continue
        if key.startswith("proposed_"):
            proposed[int(key.removeprefix("proposed_"))] = float(value)
        elif key.startswith("qty_"):
            used[int(key.removeprefix("qty_"))] = float(value)

    if not used:
        used = recipe_requirements(db, recipe, servings=servings or None).as_map()

    notes = {
        iid: f"cook adjust: proposed {proposed[iid]:g}, used {amount:g}"
        for iid, amount in used.items()
        if iid in proposed and abs(amount - proposed[iid]) > 1e-6
    }
    when = _cook_when(cooked_on)
    # The purchase has to land before the debit so the ledger's running total
    # never pretends the ingredient was used from an empty jar.
    cover_cook_shortfall(db, user.id, used, recipe_id=recipe.id, occurred_at=when)
    consume_for_recipe(
        db, user.id, used, recipe_id=recipe.id, notes=notes or None, occurred_at=when
    )

    recipe.times_cooked += 1
    previous = recipe.last_cooked_at
    if previous is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if previous is None or previous < when:
        recipe.last_cooked_at = when

    # Without an entry_id the day on the form decides which plan entry closes,
    # so logging Thursday from the recipe page still attaches to Thursday.
    if plan_entry is not None:
        attached = plan_entry if plan_entry.cooked_at is None else None
    else:
        attached = _open_plan_entry(db, user.id, recipe.id, cooked_on)
    if attached is not None:
        attached.cooked_at = when
        if servings:
            attached.servings = servings
    if servings:
        recipe.last_servings = servings
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}?cooked=1", status_code=303)
