"""Routes: shopping."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.core.audit import build_audit
from souschef.core.units import NeedsPackage, UnitConversionError
from souschef.db import get_db
from souschef.models import MealPlanEntry, ShoppingListItem, User
from souschef.planning.shopping import (
    ShoppingError,
    add_manual_item,
    live_list,
    requirements_for_audit,
    stock_ticked_items,
    unnamed_recipe_lines,
)
from souschef.web import auth
from souschef.web.common import (
    _horizon,
    _render,
    _sync_shopping,
    add_form_units,
    purchase_qty_text,
)

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


def _group_unnamed(lines: list[tuple[int, str, str]]) -> list[dict]:
    groups: list[dict] = []
    by_id: dict[int, dict] = {}
    for recipe_id, title, text in lines:
        group = by_id.get(recipe_id)
        if group is None:
            group = {"id": recipe_id, "title": title, "lines": []}
            by_id[recipe_id] = group
            groups.append(group)
        group["lines"].append(text)
    return groups


_NOT_ON_LIST = "That item is no longer on the list"


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
    # Checked rows leave their aisle for the basket. The copy still lists every row.
    by_aisle: dict[str, list] = {}
    for item in sorted(
        (item for item in visible if not item.is_checked),
        key=lambda item: (item.aisle or "", item.sort_order, item.id),
    ):
        by_aisle.setdefault(item.aisle or "Other", []).append(item)
    basket = sorted(
        (item for item in visible if item.is_checked),
        key=lambda item: (item.aisle or "", item.sort_order, item.id),
    )
    copy_groups: dict[str, list] = {}
    for item in sorted(
        visible,
        key=lambda item: (item.aisle or "", item.is_checked, item.sort_order, item.id),
    ):
        copy_groups.setdefault(item.aisle or "Other", []).append(item)
    pending_stock = sum(1 for item in visible if item.is_checked and not item.is_stocked)
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
        unnamed=_group_unnamed(unnamed_recipe_lines(db, user.id, start, end)),
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
    item = db.get(ShoppingListItem, item_id)
    if listing is None or item is None or item.shopping_list_id != listing.id:
        return _render(
            request,
            "error.html",
            status_code=404,
            title="Not found",
            detail=_NOT_ON_LIST,
            toast_message=_NOT_ON_LIST,
        )
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


@router.post("/shopping/item/{item_id}/skip")
def skip_shopping_item(
    item_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = db.get(ShoppingListItem, item_id)
    if item is not None and item.shopping_list.user_id == user.id:
        item.is_skipped = True
        item.is_checked = False
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse("/shopping", status_code=303)
