"""Routes: pantry."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from souschef.core.inventory import (
    confirm_level,
    discard_item,
    pantry_view,
    record_event,
    set_level,
    undo_discard,
)
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.spoilage import freshness_by_ingredient
from souschef.core.units import UnitConversionError
from souschef.db import get_db
from souschef.models import CanonicalIngredient, EventReason, User
from souschef.models.ingredient import IngredientCategory
from souschef.web import auth
from souschef.web.common import (
    _adhoc_units,
    _discard_notice,
    _is_htmx,
    _kitchen_unit_message,
    _on_hand,
    _render,
    _safe_next,
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
    level_error: str = "",
    wasted: int = 0,
    status_code: int = 200,
) -> HTMLResponse:
    views = pantry_view(db, user.id, include_empty=True)
    freshness = freshness_by_ingredient(db, user.id, views, date.today())
    if q.strip():
        views = _search_pantry(views, q)

    by_location: dict[str, list] = {}
    out: list = []
    for view in views:
        # A zero row is the ledger remembering that you finished it. The green
        # confidence pips describe trust in the number, so on a zero they read
        # as "in stock". A remainder below one base unit (0.4 g of cilantro)
        # is the same display: out, with the ledger left as it is.
        if view.is_staple or _on_hand(view):
            by_location.setdefault(str(view.item.location), []).append(view)
        else:
            out.append(view)

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
        level_error=level_error,
        discarded=_discard_notice(db, user, wasted),
    )


@router.get("/pantry", response_class=HTMLResponse)
def pantry_page(
    request: Request,
    q: str = "",
    wasted: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _pantry_page(request, db, user, q, wasted=wasted)


@router.post("/pantry/{ingredient_id}/level")
def update_level(
    request: Request,
    ingredient_id: int,
    quantity: float = Form(...),
    unit: str = Form(""),
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # The ingredient catalog is shared, so no ownership check applies to it --
    # the scoping happens on the pantry row `set_level` writes.
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    try:
        set_level(db, user.id, ingredient, quantity, unit=unit or None)
    except (UnitConversionError, ValueError) as exc:
        return _pantry_page(request, db, user, q, level_error=str(exc), status_code=400)
    return RedirectResponse(_after_pantry_edit(q, next_path), status_code=303)


@router.post("/pantry/{ingredient_id}/confirm")
def confirm_item(
    ingredient_id: int,
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    confirm_level(db, user.id, ingredient)
    return RedirectResponse(_after_pantry_edit(q, next_path), status_code=303)


@router.post("/pantry/{ingredient_id}/discard")
def discard_pantry_item(
    ingredient_id: int,
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    event = discard_item(db, user.id, ingredient)
    target = _after_pantry_edit(q, next_path)
    if event is not None:
        db.flush()
        target = _with_param(target, "wasted", event.id)
    return RedirectResponse(target, status_code=303)


@router.post("/pantry/undo/{event_id}")
def undo_pantry_discard(
    event_id: int,
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    undo_discard(db, user.id, event_id)
    return RedirectResponse(_after_pantry_edit(q, next_path), status_code=303)


@router.post("/pantry/add")
def add_to_pantry(
    request: Request,
    name: str = Form(...),
    quantity: float = Form(...),
    unit: str = Form(""),
    q: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # Only an *exact* catalog or learned-alias hit reuses an existing ingredient.
    # A fuzzy near-miss is not trusted here: coercing "vinegar" into "white
    # vinegar" records the wrong thing and reads as the app fighting the user.
    # Anything else is taken literally and added as a new ingredient -- the user
    # is stating what they have, not asking the catalog for permission.
    clean_name = name.strip()

    def reject(message: str) -> HTMLResponse:
        return _pantry_page(
            request,
            db,
            user,
            q,
            add_open=True,
            add_error=message,
            add_name=name,
            add_quantity=str(quantity),
            add_unit=unit,
            status_code=400,
        )

    if not clean_name:
        return reject("Ingredient name is empty.")
    if quantity <= 0:
        return reject("Amount has to be more than zero.")

    match = IngredientMatcher(db).match(clean_name)
    try:
        if match.method in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT):
            ingredient = db.get(CanonicalIngredient, match.ingredient_id)
            store_unit = unit.strip() or None
        else:
            base_unit, store_unit = _adhoc_units(unit)
            ingredient = CanonicalIngredient(
                name=clean_name, category=IngredientCategory.OTHER, base_unit=base_unit
            )
            db.add(ingredient)
            db.flush()

        record_event(
            db,
            user.id,
            ingredient,
            reason=EventReason.PURCHASE,
            delta=quantity,
            unit=store_unit,
        )
    except UnitConversionError as exc:
        return reject(_kitchen_unit_message(exc, unit))
    return RedirectResponse(_pantry_url(q), status_code=303)


@router.get("/pantry/add")
def add_to_pantry_get(_user: User = Depends(auth.current_user)) -> RedirectResponse:
    """A refresh after a failed add is a GET. Send it back to the form."""
    return RedirectResponse("/pantry", status_code=303)
