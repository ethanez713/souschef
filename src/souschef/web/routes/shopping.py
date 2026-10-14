"""Routes: shopping."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.core.audit import build_audit
from souschef.core.inventory import physical_of, set_level
from souschef.core.units import NeedsPackage, UnitConversionError, to_base
from souschef.db import get_db
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    MealPlanEntry,
    Recipe,
    RecipeIngredient,
    ShoppingList,
    ShoppingListItem,
    User,
)
from souschef.models.planning import ItemSource
from souschef.planning.shopping import (
    NEEDS_NAMING_AISLE,
    ShoppingError,
    add_manual_item,
    live_list,
    requirements_for_audit,
    stock_ticked_items,
    stockable,
)
from souschef.web import auth
from souschef.web.common import (
    _horizon,
    _kitchen_unit_message,
    _render,
    _sync_shopping,
    add_form_units,
    purchase_qty_text,
)
from souschef.web.routes.recipes import NameMatchError, assign_named_match

router = APIRouter()


def _shop_line(item: ShoppingListItem, user: User) -> str:
    system = user.unit_system or "us"
    qty = purchase_qty_text(item, system=system, show_alt=bool(user.show_alt_units))
    if qty:
        return f"{qty} {item.label}"
    return item.label


def _shopping_copy(by_aisle: dict[str, list], user: User) -> str:
    blocks = []
    for aisle, items in by_aisle.items():
        lines = [aisle, *(f"- {_shop_line(item, user)}" for item in items)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _aisle_sort_key(item: ShoppingListItem, *, checked_last: bool = False) -> tuple:
    aisle = item.aisle or "Other"
    # Needs naming is the group you deal with before walking the aisles.
    return (
        aisle != NEEDS_NAMING_AISLE,
        aisle,
        bool(item.is_checked) if checked_last else False,
        item.sort_order,
        item.id,
    )


def _group_aisles(
    items: list[ShoppingListItem], *, checked_last: bool = False
) -> dict[str, list[ShoppingListItem]]:
    grouped: dict[str, list[ShoppingListItem]] = {}
    for item in sorted(items, key=lambda row: _aisle_sort_key(row, checked_last=checked_last)):
        grouped.setdefault(item.aisle or "Other", []).append(item)
    return grouped


_NOT_ON_LIST = "That item is no longer on the list"
_HAVE_IT_NOTE = "have it"


def _missing_item(request: Request) -> HTMLResponse:
    return _render(
        request,
        "error.html",
        status_code=404,
        title="Not found",
        detail=_NOT_ON_LIST,
        toast_message=_NOT_ON_LIST,
    )


def _item_on_list(
    db: Session, listing: ShoppingList | None, item_id: int
) -> ShoppingListItem | None:
    item = db.get(ShoppingListItem, item_id)
    if listing is None or item is None or item.shopping_list_id != listing.id:
        return None
    return item


def _shop_redirect(**params: str) -> RedirectResponse:
    query = urlencode([(key, value) for key, value in params.items() if value])
    return RedirectResponse("/shopping" + (f"?{query}" if query else ""), status_code=303)


def _toast(message: str, *, undo: str = "", extra_label: str = "", extra_action: str = "") -> dict:
    return {
        "message": message,
        "undo": undo,
        "extra_label": extra_label,
        "extra_action": extra_action,
    }


def _toast_from_query(request: Request, db: Session, listing: ShoppingList | None) -> dict | None:
    params = request.query_params
    removed = params.get("removed", "")
    if removed.isdigit() and listing is not None:
        item = db.get(ShoppingListItem, int(removed))
        if item is not None and item.shopping_list_id == listing.id and item.is_skipped:
            return _toast(f"Removed {item.label}", undo=f"/shopping/item/{item.id}/unskip")
    marked = params.get("marked", "")
    if marked:
        extra_label = ""
        extra_action = ""
        always = params.get("always", "")
        if always.isdigit():
            ingredient = db.get(CanonicalIngredient, int(always))
            if ingredient is not None and not ingredient.is_staple:
                extra_label = f"Always have {ingredient.name}"
                extra_action = f"/shopping/ingredient/{ingredient.id}/always"
        return _toast(
            f"Marked {marked} as on hand",
            extra_label=extra_label,
            extra_action=extra_action,
        )
    edited = params.get("edited", "")
    if edited:
        return _toast(f"Updated {edited}")
    staple = params.get("staple", "")
    if staple:
        return _toast(f"Always have {staple}")
    named = params.get("named", "")
    if named:
        return _toast(f"Named as {named}")
    return None


def _earlier_have_it(db: Session, user_id: int, ingredient_id: int) -> bool:
    found = db.scalar(
        select(InventoryEvent.id)
        .where(
            InventoryEvent.user_id == user_id,
            InventoryEvent.ingredient_id == ingredient_id,
            InventoryEvent.reason == EventReason.MANUAL,
            InventoryEvent.note == _HAVE_IT_NOTE,
            InventoryEvent.voided_at.is_(None),
        )
        .limit(1)
    )
    return found is not None


def _on_hand_level(ingredient: CanonicalIngredient, needed_qty: float) -> float:
    """Base units Have it records: one catalog package, or the need if larger.

    The need alone would be eaten by the cook that put the line here, and the
    item would be back on next week's list.
    """
    package = 0.0
    if ingredient.package_qty and ingredient.package_unit:
        package = to_base(
            float(ingredient.package_qty),
            ingredient.package_unit,
            physical_of(ingredient),
        )
    return max(float(needed_qty or 0.0), package)


def _list_for_page(db: Session, user_id: int):
    """The live list. A build runs only when nothing is stored or the window rolled past today."""
    active = live_list(db, user_id)
    today = date.today()
    if active is None or active.covers_from is None or active.covers_from < today:
        return _sync_shopping(db, user_id)
    return active


def _render_shopping(
    request: Request,
    db: Session,
    user: User,
    *,
    add_error: str = "",
    add_name: str = "",
    add_amount: str = "",
    add_unit: str = "",
    package_word: str = "",
    package_qty: str = "",
    package_unit: str = "",
    amount_error: str = "",
    amount_for: int = 0,
    amount_value: str = "",
    amount_unit: str = "",
    name_error: str = "",
    name_for: int = 0,
    status_code: int = 200,
) -> HTMLResponse:
    start, end = _horizon()
    active = _list_for_page(db, user.id)
    has_plan = (
        db.scalar(
            select(func.count())
            .select_from(MealPlanEntry)
            .where(
                MealPlanEntry.user_id == user.id,
                MealPlanEntry.plan_date >= start,
                MealPlanEntry.plan_date <= end,
                MealPlanEntry.recipe_id.is_not(None),
                MealPlanEntry.cooked_at.is_(None),
                MealPlanEntry.skipped_at.is_(None),
            )
        )
        or 0
    ) > 0

    visible = [item for item in active.items if not item.is_skipped]
    # Checked rows leave their aisle for the basket. The copy still lists every row,
    # including ones that still need a name.
    by_aisle = _group_aisles([item for item in visible if not item.is_checked])
    basket = sorted(
        (item for item in visible if item.is_checked),
        key=_aisle_sort_key,
    )
    copy_groups = _group_aisles(visible, checked_last=True)
    pending_stock = sum(
        1 for item in visible if item.is_checked and not item.is_stocked and stockable(item)
    )
    to_get = sum(1 for item in visible if not item.is_checked and not item.is_stocked)
    unit_choices, package_caption = add_form_units(
        db, add_name, add_unit, user.unit_system or "us"
    )

    return _render(
        request,
        "shopping.html",
        status_code=status_code,
        shopping_list=active,
        items=visible,
        by_aisle=by_aisle,
        basket=basket,
        to_get=to_get,
        copy_text=_shopping_copy(copy_groups, user),
        has_plan=has_plan,
        pending_stock=pending_stock,
        added=request.query_params.getlist("added"),
        add_error=add_error,
        add_name=add_name,
        add_amount=add_amount,
        add_unit=add_unit,
        package_word=package_word,
        package_qty=package_qty,
        package_unit=package_unit,
        unit_choices=unit_choices,
        package_caption=package_caption,
        audit_count=len(build_audit(db, user.id, requirements_for_audit(db, user.id, start, end))),
        shop_toast=_toast_from_query(request, db, active),
        amount_error=amount_error,
        amount_for=amount_for,
        amount_value=amount_value,
        amount_unit=amount_unit,
        name_error=name_error,
        name_for=name_for,
    )


@router.get("/shopping", response_class=HTMLResponse)
def shopping_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render_shopping(request, db, user)


@router.post("/shopping/generate")
def generate_shopping(
    user: User = Depends(auth.current_user), db: Session = Depends(get_db)
) -> Response:
    _sync_shopping(db, user.id)
    return RedirectResponse("/shopping", status_code=303)


@router.post("/shopping/item/{item_id}/check")
def check_shopping_item(
    request: Request,
    item_id: int,
    checked: str = Form(pattern=r"^[01]$"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # A list item carries no owner of its own; ownership lives on the parent
    # list, so that is what gets checked.
    listing = live_list(db, user.id)
    item = _item_on_list(db, listing, item_id)
    if item is None:
        return _missing_item(request)
    item.is_checked = checked == "1"
    db.flush()
    return RedirectResponse("/shopping", status_code=303)


@router.post("/shopping/stock")
def stock_shopping(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    listing = live_list(db, user.id) or _sync_shopping(db, user.id)
    try:
        names = stock_ticked_items(db, user.id, listing)
    except (ShoppingError, UnitConversionError) as exc:
        db.rollback()
        return _render_shopping(request, db, user, add_error=str(exc), status_code=400)
    _sync_shopping(db, user.id)
    query = urlencode([("added", name) for name in names])
    return RedirectResponse("/shopping" + (f"?{query}" if query else ""), status_code=303)


@router.post("/shopping/add")
def add_shopping_item(
    request: Request,
    name: str = Form(""),
    amount: str = Form(""),
    unit: str = Form(""),
    package_qty: str = Form(""),
    package_unit: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    def show(message: str = "", *, word: str = "") -> HTMLResponse:
        return _render_shopping(
            request,
            db,
            user,
            add_error=message,
            add_name=name,
            add_amount=amount,
            add_unit=unit,
            package_word=word,
            package_qty=package_qty,
            package_unit=package_unit,
            status_code=400,
        )

    parsed: float | None = None
    text = amount.strip()
    if text:
        try:
            parsed = float(text)
        except ValueError:
            return show("Amount isn't a number.")
    listing = live_list(db, user.id) or _sync_shopping(db, user.id)
    try:
        add_manual_item(
            db,
            listing,
            name,
            amount=parsed,
            unit=unit.strip() or None,
            package_qty=package_qty,
            package_unit=package_unit,
        )
    except NeedsPackage as exc:
        return show(exc.detail, word=exc.word)
    except ShoppingError as exc:
        return show(str(exc))
    return RedirectResponse("/shopping", status_code=303)


def _amount_error(
    request: Request,
    db: Session,
    user: User,
    item_id: int,
    message: str,
    amount: str,
    unit: str,
) -> HTMLResponse:
    return _render_shopping(
        request,
        db,
        user,
        amount_error=message,
        amount_for=item_id,
        amount_value=amount,
        amount_unit=unit,
        status_code=400,
    )


@router.post("/shopping/item/{item_id}/have")
def have_shopping_item(
    request: Request,
    item_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = _item_on_list(db, live_list(db, user.id), item_id)
    if item is None:
        return _missing_item(request)
    ingredient = item.ingredient
    if ingredient is None:
        return _render_shopping(
            request,
            db,
            user,
            add_error=f"{item.label} isn't in the catalog.",
            status_code=400,
        )
    name = item.label
    ingredient_id = ingredient.id
    earlier = _earlier_have_it(db, user.id, ingredient_id)
    try:
        set_level(
            db,
            user.id,
            ingredient,
            _on_hand_level(ingredient, item.needed_qty),
            note=_HAVE_IT_NOTE,
        )
    except (UnitConversionError, ValueError) as exc:
        db.rollback()
        return _render_shopping(request, db, user, add_error=str(exc), status_code=400)
    _sync_shopping(db, user.id)
    return _shop_redirect(marked=name, always=str(ingredient_id) if earlier else "")


@router.post("/shopping/item/{item_id}/amount")
def edit_shopping_amount(
    request: Request,
    item_id: int,
    amount: str = Form(""),
    unit: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = _item_on_list(db, live_list(db, user.id), item_id)
    if item is None:
        return _missing_item(request)
    text = amount.strip()
    unit_text = unit.strip()
    try:
        parsed = float(text)
    except ValueError:
        return _amount_error(request, db, user, item.id, "Amount isn't a number.", amount, unit)
    if parsed <= 0:
        return _amount_error(
            request, db, user, item.id, "Amount has to be more than zero.", amount, unit
        )
    if not unit_text:
        return _amount_error(request, db, user, item.id, "Enter a unit.", amount, unit)
    if len(unit_text) > 32:
        return _amount_error(request, db, user, item.id, "Unit is too long.", amount, unit)
    item.purchase_qty = parsed
    item.purchase_unit = unit_text
    item.amount_edited = True
    db.flush()
    return _shop_redirect(edited=item.label)


@router.post("/shopping/item/{item_id}/skip")
def skip_shopping_item(
    request: Request,
    item_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = _item_on_list(db, live_list(db, user.id), item_id)
    if item is None:
        return _missing_item(request)
    kept = item.id
    item.is_skipped = True
    item.is_checked = False
    db.flush()
    _sync_shopping(db, user.id)
    return _shop_redirect(removed=str(kept))


@router.post("/shopping/item/{item_id}/unskip")
def unskip_shopping_item(
    request: Request,
    item_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = _item_on_list(db, live_list(db, user.id), item_id)
    if item is None:
        return _missing_item(request)
    item.is_skipped = False
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse("/shopping", status_code=303)


def _unnamed_recipe_lines(db: Session, user_id: int, raw_text: str) -> list[RecipeIngredient]:
    """Every still-unnamed line of this kitchen whose text matches the row."""
    return list(
        db.scalars(
            select(RecipeIngredient)
            .join(Recipe)
            .where(
                Recipe.user_id == user_id,
                RecipeIngredient.ingredient_id.is_(None),
                func.lower(RecipeIngredient.raw_text) == raw_text.casefold(),
            )
            .order_by(Recipe.id, RecipeIngredient.position)
        )
    )


@router.post("/shopping/item/{item_id}/name")
def name_shopping_item(
    request: Request,
    item_id: int,
    name: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = _item_on_list(db, live_list(db, user.id), item_id)
    if item is None:
        return _missing_item(request)
    if item.ingredient_id is not None or item.source != ItemSource.RECIPE:
        return _render_shopping(
            request,
            db,
            user,
            add_error="Only an unnamed recipe line can be named from here.",
            status_code=400,
        )
    lines = _unnamed_recipe_lines(db, user.id, item.label)
    if not lines:
        return _render_shopping(
            request,
            db,
            user,
            name_error="That line is no longer on a recipe.",
            name_for=item.id,
            status_code=400,
        )
    try:
        ingredient = assign_named_match(db, item.label, name, lines)
    except NameMatchError as exc:
        return _render_shopping(
            request,
            db,
            user,
            name_error=str(exc),
            name_for=item.id,
            status_code=400,
        )
    except UnitConversionError as exc:
        return _render_shopping(
            request,
            db,
            user,
            name_error=_kitchen_unit_message(exc, lines[0].unit or ""),
            name_for=item.id,
            status_code=400,
        )
    was_ticked = item.is_checked
    rebuilt = _sync_shopping(db, user.id)
    if was_ticked:
        for row in rebuilt.items:
            if row.ingredient_id == ingredient.id and not row.is_skipped:
                row.is_checked = True
    return _shop_redirect(named=ingredient.name)


@router.post("/shopping/ingredient/{ingredient_id}/always")
def always_have_ingredient(
    request: Request,
    ingredient_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        return _render(
            request,
            "error.html",
            status_code=404,
            title="Not found",
            detail="No such ingredient",
        )
    ingredient.is_staple = True
    db.flush()
    _sync_shopping(db, user.id)
    return _shop_redirect(staple=ingredient.name)
