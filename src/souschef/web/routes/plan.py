"""Routes: plan."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified

from souschef.db import get_db
from souschef.models import MealPlanEntry, MealSlot, Recipe, User
from souschef.planning.next_step import readiness_chip
from souschef.planning.suggest import Surface, by_surface, suggest
from souschef.web import auth
from souschef.web.common import (
    MAX_SERVINGS,
    PAST_DAYS,
    PLAN_DAYS,
    _cook_toast,
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
    (Surface.REACH, "Missing more"),
)

# Past this many recipes the day sheet offers a filter. Fewer, and the list
# fits the sheet without one.
_FILTER_AFTER = 8
_TITLE_MAX = 200


def _recipe_groups(
    db: Session, user_id: int, entries: list[MealPlanEntry], today: date
) -> list[tuple[str, list[tuple[Recipe, str]]]]:
    """Every recipe for the day pickers, grouped by how cookable it is now."""
    planned = _planned_days(entries, today)
    grouped = by_surface(suggest(db, user_id, limit=100_000))
    return [
        (
            label,
            [(s.recipe, _picker_note(s, planned)) for s in grouped[surface]],
        )
        for surface, label in _RECIPE_GROUPS
        if grouped[surface]
    ]


def _picker_note(suggestion: object, planned: dict[int, str]) -> str:
    """Chip, then the days this recipe is already on."""
    recipe = suggestion.recipe  # type: ignore[attr-defined]
    bits: list[str] = []
    chip = readiness_chip(suggestion)
    if chip != "Ready":
        bits.append(chip)
    if recipe.id in planned:
        bits.append(f"planned {planned[recipe.id]}")
    if not bits:
        return ""
    return " · " + " · ".join(bits)


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


def _capped_title(value: str) -> str:
    """Freeform titles ride in the undo URL. Cap them and drop control characters."""
    cleaned = "".join(ch for ch in value.strip() if ch.isprintable())
    return cleaned[:_TITLE_MAX]


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_servings(value: str) -> int | None:
    if not value.isdigit():
        return None
    servings = int(value)
    if not 1 <= servings <= MAX_SERVINGS:
        return None
    return servings


def _parse_slot(value: str) -> MealSlot | None:
    try:
        return MealSlot(value)
    except ValueError:
        return None


def _restore_params(params: Mapping[str, str]) -> dict[str, str] | None:
    """The bits Undo needs to put a meal back, or None when any of them is bad."""
    day = _parse_day(params.get("date", ""))
    servings = _parse_servings(params.get("servings", ""))
    slot = _parse_slot(params.get("slot", ""))
    if day is None or servings is None or slot is None:
        return None
    fields = {
        "date": day.isoformat(),
        "servings": str(servings),
        "slot": slot.value,
    }
    recipe_id = params.get("recipe", "")
    if recipe_id.isdigit():
        fields["recipe"] = str(int(recipe_id))
        return fields
    title = _capped_title(params.get("title", ""))
    if not title:
        return None
    fields["title"] = title
    return fields


def _toast(
    message: str,
    undo: str = "",
    fields: list[tuple[str, str]] | None = None,
) -> dict[str, str | list[tuple[str, str]]]:
    return {"message": message, "undo": undo, "fields": fields or []}


def _plan_location(**params: str) -> str:
    query = urlencode([(key, value) for key, value in params.items() if value])
    return "/plan" + (f"?{query}" if query else "")


def _entry_title(entry: MealPlanEntry) -> str:
    if entry.recipe is not None:
        return entry.recipe.title
    return entry.freeform_title or "Meal"


def _plan_toast(
    request: Request, db: Session, user: User
) -> dict[str, str | list[tuple[str, str]]] | None:
    """Toast copy from the database. A removed note's title is the one string from the URL."""
    params = request.query_params
    added = params.get("added", "")
    if added:
        if not added.isdigit():
            return None
        entry = db.get(MealPlanEntry, int(added))
        if entry is None or entry.user_id != user.id:
            return None
        when = entry.plan_date.strftime("%a %-d %b")
        return _toast(f"Added {_entry_title(entry)} to {when}", f"/plan/{entry.id}/remove")
    moved = params.get("moved", "")
    if moved:
        if not moved.isdigit():
            return None
        entry = db.get(MealPlanEntry, int(moved))
        origin = _parse_day(params.get("from", ""))
        if entry is None or entry.user_id != user.id or origin is None:
            return None
        when = entry.plan_date.strftime("%a %-d %b")
        return _toast(
            f"Moved {_entry_title(entry)} to {when}",
            f"/plan/{entry.id}/move",
            [("plan_date", origin.isoformat())],
        )
    fields = _restore_params(params)
    if fields is None:
        return None
    if "recipe" in fields:
        recipe = db.get(Recipe, int(fields["recipe"]))
        if recipe is None or recipe.user_id != user.id:
            return None
        title = recipe.title
    else:
        title = fields["title"]
    return _toast(f"Removed {title}", "/plan/restore?" + urlencode(fields))


@router.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    skipped: int = 0,
    blocked: int = 0,
    cooked_cook: int = 0,
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
    groups = _recipe_groups(db, user.id, entries, today)  # type: ignore[arg-type]

    return _render(
        request,
        "plan.html",
        by_day=by_day,
        days=days,
        today=today,
        has_meals=any(by_day.values()),
        past_days=[day for day in shown if day < today and by_day[day]],
        cook_toast=_cook_toast(db, user, cooked_cook, "/plan"),
        recipe_groups=groups,
        show_recipe_filter=sum(len(options) for _, options in groups) > _FILTER_AFTER,
        week_end=week_end,
        skipped=bool(skipped),
        blocked=bool(blocked),
        plan_toast=_plan_toast(request, db, user),
    )


def _plan_recipe(
    db: Session,
    user_id: int,
    recipe_id: int,
    day: date,
    servings: int,
    slot: MealSlot = MealSlot.DINNER,
) -> MealPlanEntry | None:
    """Plan a recipe on a day. None when it is already there.

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
        return None
    entry = MealPlanEntry(
        user_id=user_id,
        plan_date=day,
        slot=slot,
        recipe_id=recipe_id,
        servings=servings,
    )
    db.add(entry)
    return entry


def _plan_note(
    db: Session,
    user_id: int,
    day: date,
    title: str,
    *,
    servings: int | None = None,
    slot: MealSlot = MealSlot.DINNER,
) -> MealPlanEntry | None:
    """A night off. None when that note is already on the day."""
    taken = db.scalar(
        select(MealPlanEntry.id).where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.plan_date == day,
            MealPlanEntry.freeform_title == title,
        )
    )
    if taken is not None:
        return None
    entry = MealPlanEntry(
        user_id=user_id,
        plan_date=day,
        slot=slot,
        freeform_title=title,
    )
    if servings is not None:
        entry.servings = servings
    db.add(entry)
    return entry


def _conflicts(db: Session, entry: MealPlanEntry, day: date) -> bool:
    query = select(MealPlanEntry.id).where(
        MealPlanEntry.user_id == entry.user_id,
        MealPlanEntry.plan_date == day,
        MealPlanEntry.id != entry.id,
    )
    if entry.recipe_id is not None:
        query = query.where(MealPlanEntry.recipe_id == entry.recipe_id)
    else:
        query = query.where(MealPlanEntry.freeform_title == entry.freeform_title)
    return db.scalar(query) is not None


@router.post("/plan/add")
async def add_to_plan(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    recipe_id = int(form.get("recipe_id") or 0)  # type: ignore[arg-type]
    # Validate the recipe is theirs before planning it: without this, posting an
    # arbitrary id would put another user's recipe on your calendar, and the
    # plan page would then render its title.
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    servings = min(
        int(form.get("servings") or 0),  # type: ignore[arg-type]
        MAX_SERVINGS,
    ) or _default_servings(recipe)
    skipped = False
    added_entry: MealPlanEntry | None = None
    added_days: list[date] = []
    for raw in form.getlist("plan_date"):
        value = str(raw).strip()
        if not value:
            continue
        day = date.fromisoformat(value)
        entry = _plan_recipe(db, user.id, recipe_id, day, servings)
        if entry is None:
            skipped = True
        else:
            added_days.append(day)
            if added_entry is None:
                added_entry = entry
    # The recipe page asks to come back with a toast. Everywhere else stays on the plan.
    nxt = _safe_next(str(form.get("next") or ""))
    path = nxt.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    back = path == f"/recipes/{recipe.id}"
    if added_entry is not None:
        db.flush()
        _sync_shopping(db, user.id)
        if back:
            planned = ",".join(day.isoformat() for day in added_days)
            return RedirectResponse(f"/recipes/{recipe.id}?planned={planned}", status_code=303)
        location = f"/plan?added={added_entry.id}"
        if skipped:
            location += "&skipped=1"
        return RedirectResponse(location, status_code=303)
    if back:
        return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)
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
    entry: MealPlanEntry | None = None
    if kind == "r" and value.isdigit():
        recipe = _owned(db, Recipe, int(value), user, "recipe")
        entry = _plan_recipe(db, user.id, recipe.id, plan_date, _default_servings(recipe))
    elif kind == "n" and value in NOTE_TITLES:
        entry = _plan_note(db, user.id, plan_date, value)
    if entry is not None:
        db.flush()
        _sync_shopping(db, user.id)
        return RedirectResponse(f"/plan?added={entry.id}", status_code=303)
    return RedirectResponse(f"/plan#d-{plan_date.isoformat()}", status_code=303)


@router.post("/plan/restore")
def restore_plan_entry(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Put back the meal a remove-toast Undo describes. The entry itself is already gone."""
    fields = _restore_params(request.query_params)
    if fields is None:
        return RedirectResponse("/plan", status_code=303)
    day = date.fromisoformat(fields["date"])
    servings = int(fields["servings"])
    slot = MealSlot(fields["slot"])
    if "recipe" in fields:
        recipe = _owned(db, Recipe, int(fields["recipe"]), user, "recipe")
        entry = _plan_recipe(db, user.id, recipe.id, day, servings, slot)
    else:
        entry = _plan_note(
            db, user.id, day, fields["title"], servings=servings, slot=slot
        )
    if entry is None:
        return RedirectResponse("/plan", status_code=303)
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/plan?added={entry.id}", status_code=303)


@router.post("/plan/{entry_id}/move")
def move_plan_entry(
    entry_id: int,
    plan_date: date = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
    if entry.cooked_at is not None or entry.skipped_at is not None:
        raise HTTPException(status_code=404, detail="No such plan entry")
    today, week_end = _horizon()
    if (
        plan_date == entry.plan_date
        or plan_date < today - timedelta(days=PAST_DAYS)
        or plan_date > week_end
    ):
        return RedirectResponse("/plan", status_code=303)
    if _conflicts(db, entry, plan_date):
        return RedirectResponse("/plan?blocked=1", status_code=303)
    origin = entry.plan_date.isoformat()
    entry.plan_date = plan_date
    # Keep updated_at. The recipe picker treats it as "recently planned".
    flag_modified(entry, "updated_at")
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/plan?moved={entry.id}&from={origin}", status_code=303)


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
    if entry is None or entry.user_id != user.id:
        return RedirectResponse("/plan", status_code=303)
    params = {
        "date": entry.plan_date.isoformat(),
        "servings": str(entry.servings),
        "slot": entry.slot.value,
    }
    if entry.recipe_id is not None:
        params["recipe"] = str(entry.recipe_id)
    else:
        title = _capped_title(entry.freeform_title or "")
        if title:
            params["title"] = title
    db.delete(entry)
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(_plan_location(**params), status_code=303)


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
