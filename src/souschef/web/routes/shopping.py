"""Routes: shopping."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.core.audit import build_audit
from souschef.core.units import UnitConversionError
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
)

router = APIRouter()


def _shop_line(item: ShoppingListItem) -> str:
    label = item.label
    qty = item.purchase_qty
    unit = (item.purchase_unit or "").strip()
    if not qty:
        return label
    n = int(round(qty))
    if unit == "count" and label.casefold() == "garlic":
        word = "clove" if n == 1 else "cloves"
        return f"{n} {word} {label}"
    if unit == "count":
        return f"{n}× {label}"
    if unit:
        return f"{n} {unit} {label}"
    return label


def _shopping_copy(by_aisle: dict[str, list]) -> str:
    blocks = []
    for aisle, items in by_aisle.items():
        lines = [aisle, *(f"- {_shop_line(item)}" for item in items)]
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


def _render_shopping(
    request: Request,
    db: Session,
    user: User,
    *,
    add_error: str = "",
    add_name: str = "",
    add_amount: str = "",
    add_unit: str = "",
    status_code: int = 200,
    rebuild: bool = True,
) -> HTMLResponse:
    start, end = _horizon()
    if rebuild:
        active = _sync_shopping(db, user.id)
    else:
        active = live_list(db, user.id) or _sync_shopping(db, user.id)
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
    by_aisle: dict[str, list] = {}
    for item in sorted(visible, key=lambda i: (i.aisle or "", i.is_checked, i.sort_order)):
        by_aisle.setdefault(item.aisle or "Other", []).append(item)
    pending_stock = sum(1 for item in visible if item.is_checked and not item.is_stocked)

    return _render(
        request,
        "shopping.html",
        status_code=status_code,
        shopping_list=active,
        items=visible,
        by_aisle=by_aisle,
        copy_text=_shopping_copy(by_aisle),
        has_plan=has_plan,
        pending_stock=pending_stock,
        unnamed=_group_unnamed(unnamed_recipe_lines(db, user.id, start, end)),
        added=request.query_params.getlist("added"),
        add_error=add_error,
        add_name=add_name,
        add_amount=add_amount,
        add_unit=add_unit,
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


@router.post("/shopping/item/{item_id}/toggle")
def toggle_shopping_item(
    item_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # A list item carries no owner of its own; ownership lives on the parent
    # list, so that is what gets checked.
    item = db.get(ShoppingListItem, item_id)
    if item is not None and item.shopping_list.user_id == user.id:
        item.is_checked = not item.is_checked
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
    query = urlencode([("added", name) for name in names])
    return RedirectResponse("/shopping" + (f"?{query}" if query else ""), status_code=303)


@router.post("/shopping/add")
def add_shopping_item(
    request: Request,
    name: str = Form(""),
    amount: str = Form(""),
    unit: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    parsed: float | None = None
    text = amount.strip()
    if text:
        try:
            parsed = float(text)
        except ValueError:
            return _render_shopping(
                request,
                db,
                user,
                add_error="Amount isn't a number.",
                add_name=name,
                add_amount=amount,
                add_unit=unit,
                status_code=400,
            )
    listing = live_list(db, user.id) or _sync_shopping(db, user.id)
    try:
        add_manual_item(db, listing, name, amount=parsed, unit=unit.strip() or None)
    except ShoppingError as exc:
        return _render_shopping(
            request,
            db,
            user,
            add_error=str(exc),
            add_name=name,
            add_amount=amount,
            add_unit=unit,
            status_code=400,
            rebuild=False,
        )
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
    return RedirectResponse("/shopping", status_code=303)
