"""Routes: pantry."""

from __future__ import annotations

import math
from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import (
    confirm_level,
    discard_item,
    get_or_create_item,
    pantry_view,
    physical_of,
    record_event,
    save_package_size,
    undo_event,
)
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.spoilage import freshness_by_ingredient
from souschef.core.units import (
    InputUnitError,
    NeedsPackage,
    UnitConversionError,
    count_noun_measure,
    entry_label,
    format_entry_amount,
    parse_input_unit,
    parse_package_answer,
    resolve_entry,
    resolve_entry_unit,
    to_base,
)
from souschef.db import get_db
from souschef.models import CanonicalIngredient, EventReason, InventoryEvent, PantryItem, User
from souschef.models.ingredient import IngredientCategory
from souschef.web import auth
from souschef.web.common import (
    _adhoc_units,
    _change_toast,
    _is_htmx,
    _kitchen_unit_message,
    _on_hand,
    _render,
    _safe_next,
    _sync_shopping,
    add_form_units,
)

router = APIRouter()


def _pantry_url(q: str) -> str:
    """Back to the pantry, keeping the active search so an edit doesn't drop you
    back to the full list."""
    return f"/pantry?q={quote_plus(q)}" if q.strip() else "/pantry"


def _after_pantry_edit(q: str, next_path: str) -> str:
    return _safe_next(next_path) if next_path else _pantry_url(q)


def _with_param(url: str, key: str, value: object) -> str:
    base, _, fragment = url.partition("#")
    joined = f"{base}{'&' if '?' in base else '?'}{key}={value}"
    return f"{joined}#{fragment}" if fragment else joined


# Below this rapidfuzz score a name is not offered as a match. Substring hits
# always pass; the fuzzy floor only catches typos ("chikn" -> "chicken").
PANTRY_SEARCH_CUTOFF = 70

# Fridge, freezer, then the cupboard. Out follows these, and is not a location.
_SECTION_ORDER = ("fridge", "freezer", "pantry", "other")


def _search_pantry(views: list, q: str) -> list:
    """Filter and rank pantry rows against a live search box.

    A substring hit is treated as certain and floats to the top; everything else
    is scored fuzzily so a mistyped query still finds the item. Ties keep the
    incoming alphabetical order.
    """
    needle = q.strip().lower()
    scored = []
    for view in views:
        name = view.ingredient.name.lower()
        if needle in name:
            score = 100.0
        else:
            # partial_ratio treats a shared prefix as a match ("lentil" → lemon).
            # Compare the whole name and each word so a typo still hits.
            score = max(
                fuzz.ratio(needle, name),
                *(fuzz.ratio(needle, token) for token in name.split()),
            )
        if score >= PANTRY_SEARCH_CUTOFF:
            scored.append((score, view))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [view for _, view in scored]


def _group_pantry(views: list) -> tuple[dict[str, list], list, int]:
    """Fridge, freezer, pantry, then whatever is left. Staples collapse to a count.

    Out is known stock at zero (a remainder under one base unit counts as zero
    on screen). An ingredient touched only by cooking is unknown and is omitted.
    """
    grouped = {name: [] for name in _SECTION_ORDER}
    staples = 0
    out: list = []
    for view in views:
        if view.is_staple:
            staples += 1
            continue
        if not view.is_known:
            continue
        if _on_hand(view):
            grouped.setdefault(str(view.item.location), []).append(view)
        else:
            out.append(view)
    return {name: rows for name, rows in grouped.items() if rows}, out, staples


def _stock_change(db: Session, user: User, event_id: int) -> dict | None:
    """The undo toast for a stock edit, including a discard.

    Purchases, confirms and corrections already come from the shared helper.
    A waste event is only toasted from the pantry, as "Threw out <name>".
    """
    change = _change_toast(db, user, event_id)
    if change is not None or not event_id:
        return change
    event = db.get(InventoryEvent, event_id)
    if event is None or event.user_id != user.id or event.voided_at is not None:
        return None
    if event.reason != EventReason.WASTE:
        return None
    ingredient = db.get(CanonicalIngredient, event.ingredient_id)
    if ingredient is None:
        return None
    return {"message": f"Threw out {ingredient.name}", "event_id": event.id}


def _floor_note(db: Session, user: User, ingredient_id: int) -> str:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        return ""
    item = db.scalar(
        select(PantryItem).where(
            PantryItem.user_id == user.id, PantryItem.ingredient_id == ingredient.id
        )
    )
    if item is None or item.restock_threshold is None:
        return ""
    system = user.unit_system or "us"
    if system not in ("us", "metric"):
        system = "us"
    amount = format_entry_amount(item.restock_threshold, ingredient, system)
    label = entry_label(ingredient, system, item.restock_threshold)
    return f"Keep at least {amount} {label} {ingredient.name}"


def _pantry_note(db: Session, user: User, *, floor: int, cleared: int, staple: int) -> str:
    """Words for an action that is not a ledger row, so they are not taken from the URL."""
    if floor:
        return _floor_note(db, user, floor)
    if cleared:
        ingredient = db.get(CanonicalIngredient, cleared)
        return f"Cleared the minimum for {ingredient.name}" if ingredient is not None else ""
    if staple:
        ingredient = db.get(CanonicalIngredient, staple)
        if ingredient is not None and ingredient.is_staple:
            return f"Always have {ingredient.name}"
    return ""


def _floor_amount(quantity: str) -> float:
    text = quantity.strip()
    if not text:
        raise ValueError("Enter a number.")
    try:
        value = float(text)
    except ValueError:
        raise ValueError("Enter a number.") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Amount has to be more than zero.")
    return value


def _pantry_page(
    request: Request,
    db: Session,
    user: User,
    q: str,
    *,
    add_open: bool = False,
    add_error: str = "",
    add_name: str = "",
    add_quantity: str = "",
    add_unit: str = "",
    package_word: str = "",
    package_qty: str = "",
    package_unit: str = "",
    level_error: str = "",
    changed: int = 0,
    floor: int = 0,
    cleared: int = 0,
    staple: int = 0,
    status_code: int = 200,
) -> HTMLResponse:
    views = pantry_view(db, user.id, include_empty=True)
    freshness = freshness_by_ingredient(db, user.id, views, date.today())
    if q.strip():
        views = _search_pantry(views, q)

    by_location, out, staples = _group_pantry(views)

    unit_choices, package_caption = add_form_units(
        db, add_name, add_unit, user.unit_system or "us"
    )
    # A live-search keystroke swaps in only the list; a full navigation renders
    # the page around it.
    template = "pantry_list.html" if _is_htmx(request) else "pantry.html"
    return _render(
        request,
        template,
        status_code=status_code,
        by_location=by_location,
        out=out,
        freshness=freshness,
        query=q,
        add_open=add_open,
        add_error=add_error,
        add_name=add_name,
        add_quantity=add_quantity,
        add_unit=add_unit,
        package_word=package_word,
        package_qty=package_qty,
        package_unit=package_unit,
        unit_choices=unit_choices,
        package_caption=package_caption,
        level_error=level_error,
        change=_stock_change(db, user, changed),
        note=_pantry_note(db, user, floor=floor, cleared=cleared, staple=staple),
        staples=staples,
    )


@router.get("/pantry", response_class=HTMLResponse)
def pantry_page(
    request: Request,
    q: str = "",
    changed: int = 0,
    again: int = 0,
    floor: int = 0,
    cleared: int = 0,
    staple: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _pantry_page(
        request,
        db,
        user,
        q,
        add_open=bool(again),
        changed=changed,
        floor=floor,
        cleared=cleared,
        staple=staple,
    )


def _record_check(
    db: Session,
    user: User,
    ingredient: CanonicalIngredient,
    quantity: str,
    shown: str,
    unit: str | None = None,
) -> InventoryEvent:
    text = quantity.strip()
    if not text:
        raise ValueError("Enter a number.")
    # The box is prefilled with a display string. Matching that string is a
    # confirm; parsing both and comparing floats would turn a tick into a
    # zero-delta correction.
    if text == shown.strip():
        return confirm_level(db, user.id, ingredient)
    try:
        value = float(text)
    except ValueError:
        raise ValueError("Enter a number.") from None
    if not math.isfinite(value):
        raise ValueError("Enter a number.")
    if value < 0:
        raise ValueError("Amount has to be zero or more.")
    return confirm_level(
        db,
        user.id,
        ingredient,
        value,
        unit=resolve_entry_unit(ingredient, user.unit_system or "us", unit),
    )


def _check_error(
    request: Request,
    db: Session,
    user: User,
    q: str,
    next_path: str,
    message: str,
) -> HTMLResponse:
    target = _safe_next(next_path) if next_path.strip() else ""
    path = target.split("?", 1)[0].split("#", 1)[0] if target else ""
    if path == "/":
        from souschef.web.routes.home import render_home

        page = render_home(request, db, user, form_error=message)
        page.status_code = 400
        return page
    # htmx 2 does not swap a 4xx into #pantry-list, so the notice only shows at 200.
    status = 200 if _is_htmx(request) else 400
    return _pantry_page(request, db, user, q, level_error=message, status_code=status)


@router.post("/pantry/{ingredient_id}/check")
def check_item(
    request: Request,
    ingredient_id: int,
    quantity: str = Form(""),
    shown: str = Form(""),
    unit: str = Form(""),
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    try:
        event = _record_check(db, user, ingredient, quantity, shown, unit)
    except (UnitConversionError, ValueError) as exc:
        return _check_error(request, db, user, q, next_path, str(exc))
    _sync_shopping(db, user.id)
    target = _with_param(_after_pantry_edit(q, next_path), "changed", event.id)
    return RedirectResponse(target, status_code=303)


@router.post("/pantry/undo/{event_id}")
def undo_pantry_event(
    event_id: int,
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    if undo_event(db, user.id, event_id) is not None:
        _sync_shopping(db, user.id)
    return RedirectResponse(_after_pantry_edit(q, next_path), status_code=303)


def _catalog_ingredient(db: Session, ingredient_id: int) -> CanonicalIngredient:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    return ingredient


@router.post("/pantry/{ingredient_id}/discard")
def discard_stock(
    ingredient_id: int,
    q: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = _catalog_ingredient(db, ingredient_id)
    event = discard_item(db, user.id, ingredient)
    if event is None:
        return RedirectResponse(_pantry_url(q), status_code=303)
    _sync_shopping(db, user.id)
    return RedirectResponse(_with_param(_pantry_url(q), "changed", event.id), status_code=303)


@router.post("/pantry/{ingredient_id}/floor")
def set_restock_floor(
    request: Request,
    ingredient_id: int,
    quantity: str = Form(""),
    unit: str = Form(""),
    clear: str = Form(""),
    q: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = _catalog_ingredient(db, ingredient_id)
    item = get_or_create_item(db, user.id, ingredient)
    if clear:
        item.restock_threshold = None
        db.flush()
        _sync_shopping(db, user.id)
        return RedirectResponse(
            _with_param(_pantry_url(q), "cleared", ingredient.id), status_code=303
        )
    try:
        item.restock_threshold = to_base(
            _floor_amount(quantity),
            resolve_entry_unit(ingredient, user.unit_system or "us", unit),
            physical_of(ingredient),
        )
    except (UnitConversionError, ValueError) as exc:
        status = 200 if _is_htmx(request) else 400
        return _pantry_page(request, db, user, q, level_error=str(exc), status_code=status)
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(_with_param(_pantry_url(q), "floor", ingredient.id), status_code=303)


@router.post("/pantry/{ingredient_id}/staple")
def mark_always_have(
    ingredient_id: int,
    q: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Always have is a shared catalog flag. One kitchen, so write it directly."""
    ingredient = _catalog_ingredient(db, ingredient_id)
    item = db.scalar(
        select(PantryItem).where(
            PantryItem.user_id == user.id, PantryItem.ingredient_id == ingredient.id
        )
    )
    # A staple is never shopped, including from a restock floor set a moment ago.
    if item is not None:
        item.restock_threshold = None
    ingredient.is_staple = True
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(_with_param(_pantry_url(q), "staple", ingredient.id), status_code=303)


def _typed_amount(quantity: float) -> str:
    if quantity.is_integer():
        return str(int(quantity))
    return str(quantity)


def _has_package(ingredient: CanonicalIngredient | None) -> bool:
    return bool(ingredient and ingredient.package_qty and ingredient.package_unit)


@router.post("/pantry/add")
def add_to_pantry(
    request: Request,
    name: str = Form(...),
    quantity: float = Form(...),
    unit: str = Form(""),
    q: str = Form(""),
    package_qty: str = Form(""),
    package_unit: str = Form(""),
    again: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # Only an *exact* catalog or learned-alias hit reuses an existing ingredient.
    # A fuzzy near-miss is not trusted here: coercing "vinegar" into "white
    # vinegar" records the wrong thing and reads as the app fighting the user.
    # Anything else is taken literally and added as a new ingredient -- the user
    # is stating what they have, not asking the catalog for permission.
    clean_name = name.strip()
    shown_amount = _typed_amount(quantity)

    def show(
        message: str = "",
        *,
        word: str = "",
    ) -> HTMLResponse:
        return _pantry_page(
            request,
            db,
            user,
            q,
            add_open=True,
            add_error=message,
            add_name=name,
            add_quantity=shown_amount,
            add_unit=unit,
            package_word=word,
            package_qty=package_qty,
            package_unit=package_unit,
            status_code=400,
        )

    if not clean_name:
        return show("Ingredient name is empty.")
    if quantity <= 0:
        return show("Amount has to be more than zero.")

    match = IngredientMatcher(db).match(clean_name)
    ingredient: CanonicalIngredient | None = None
    if match.method in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT):
        ingredient = db.get(CanonicalIngredient, match.ingredient_id)

    typed = unit
    count_noun = ingredient.count_noun if ingredient is not None else None
    if count_noun_measure(unit, count_noun):
        typed = "count"
        pack = None
    else:
        try:
            pack = parse_input_unit(unit)[1]
        except InputUnitError as exc:
            return show(str(exc))

    # A package word with no size is a question, not a row. Nothing is written
    # until the follow-up says how big one of them is. A size sent with the add
    # (the first answer, or Change) is stored on the ingredient first, then the
    # amount converts through it.
    try:
        answer = parse_package_answer(package_qty, package_unit)
    except InputUnitError as exc:
        return show(str(exc), word=pack or "package")
    if answer is not None:
        try:
            ingredient = save_package_size(db, ingredient, clean_name, answer[0], answer[1])
        except (UnitConversionError, InputUnitError) as exc:
            db.rollback()
            return show(str(exc), word=pack or "package")
    elif pack and pack != "dozen" and not _has_package(ingredient):
        return show(word=pack)

    try:
        resolved = resolve_entry(
            quantity,
            typed,
            package_qty=ingredient.package_qty if ingredient is not None else None,
            package_unit=ingredient.package_unit if ingredient is not None else None,
        )
        if ingredient is None:
            base_unit, _store_unit = _adhoc_units(resolved.unit or "")
            ingredient = CanonicalIngredient(
                name=clean_name, category=IngredientCategory.OTHER, base_unit=base_unit
            )
            db.add(ingredient)
            db.flush()
        event = record_event(
            db,
            user.id,
            ingredient,
            reason=EventReason.PURCHASE,
            delta=resolved.quantity,
            unit=resolved.unit,
        )
    except NeedsPackage as exc:
        db.rollback()
        return show(exc.detail, word=exc.word)
    except UnitConversionError as exc:
        db.rollback()
        if pack:
            return show(str(exc), word=pack)
        return show(_kitchen_unit_message(exc, unit))
    _sync_shopping(db, user.id)
    target = _pantry_url(q)
    if again:
        target = _with_param(target, "again", 1)
    return RedirectResponse(_with_param(target, "changed", event.id), status_code=303)


@router.get("/pantry/add")
def add_to_pantry_get(_user: User = Depends(auth.current_user)) -> RedirectResponse:
    """A refresh after a failed add is a GET. Send it back to the form."""
    return RedirectResponse("/pantry", status_code=303)
