"""Routes: cook."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.cooking import record_cook, undo_cook
from souschef.core.inventory import pantry_view, physical_of
from souschef.core.recipes import recipe_requirements
from souschef.core.units import entry_unit, format_entry_amount, resolve_entry_unit, to_base
from souschef.db import get_db
from souschef.models import CanonicalIngredient, Cook, MealPlanEntry, Recipe, User
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
    servings = min(max(servings, 1), MAX_SERVINGS)
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
        short_count=sum(1 for row in debits if row["over"]),
        max_servings=MAX_SERVINGS,
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
    servings = min(int(form.get("servings") or 0), MAX_SERVINGS)  # type: ignore[arg-type]
    entry_id = int(form.get("entry_id") or 0)  # type: ignore[arg-type]
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
    posted: dict[int, float] = {}
    posted_units: dict[int, str] = {}
    for key, raw in form.items():
        value = str(raw).strip()
        if not value:
            continue
        if key.startswith("proposed_"):
            proposed[int(key.removeprefix("proposed_"))] = float(value)
        elif key.startswith("qty_"):
            posted[int(key.removeprefix("qty_"))] = float(value)
        elif key.startswith("unit_"):
            posted_units[int(key.removeprefix("unit_"))] = value

    system = user.unit_system or "us"
    if not posted:
        used = recipe_requirements(db, recipe, servings=servings or None).as_map()
        notes: dict[int, str] = {}
    else:
        # The box shows one entry unit. proposed_* stays in base units, so an
        # edit is a posted number that is not that same unit's rounded amount.
        used = {}
        notes = {}
        for ingredient_id, amount in posted.items():
            ingredient = db.get(CanonicalIngredient, ingredient_id)
            if ingredient is None:
                continue
            parse_unit = resolve_entry_unit(
                ingredient, system, posted_units.get(ingredient_id)
            )
            base = to_base(amount, parse_unit, physical_of(ingredient))
            used[ingredient_id] = base
            if ingredient_id not in proposed:
                continue
            posted_text = f"{amount:.6f}".rstrip("0").rstrip(".") or "0"
            shown = format_entry_amount(proposed[ingredient_id], ingredient, system)
            shown_unit = entry_unit(ingredient, system, proposed[ingredient_id])
            if posted_text != shown or parse_unit != shown_unit:
                notes[ingredient_id] = (
                    f"cook adjust: proposed {proposed[ingredient_id]:g}, used {base:g}"
                )
    when = _cook_when(cooked_on)
    # Without an entry_id the day on the form decides which plan entry closes,
    # so logging Thursday from the recipe page still attaches to Thursday.
    if plan_entry is not None:
        attached = plan_entry if plan_entry.cooked_at is None else None
    else:
        attached = _open_plan_entry(db, user.id, recipe.id, cooked_on)
    record_cook(
        db, user.id, recipe, used, when=when, servings=servings, plan_entry=attached, notes=notes
    )
    if servings:
        recipe.last_servings = servings
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}?cooked=1", status_code=303)


@router.post("/recipes/{recipe_id}/cooks/{cook_id}/undo")
def undo_marked_cook(
    recipe_id: int,
    cook_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    cook = db.get(Cook, cook_id)
    if cook is None or cook.user_id != user.id or cook.recipe_id != recipe.id:
        raise HTTPException(status_code=404, detail="No such cook")
    undo_cook(db, user.id, cook.id)
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)
