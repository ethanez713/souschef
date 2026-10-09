"""Routes: audit."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from souschef.core.audit import build_audit
from souschef.core.inventory import confirm_level
from souschef.core.units import UnitConversionError
from souschef.db import get_db
from souschef.models import CanonicalIngredient, User
from souschef.planning.shopping import requirements_for_audit
from souschef.web import auth
from souschef.web.common import (
    _horizon,
    _render,
)

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The pre-shop check: the few things worth physically verifying."""
    today, week_end = _horizon()
    requirements = requirements_for_audit(db, user.id, today, week_end)
    candidates = build_audit(db, user.id, requirements)
    return _render(
        request,
        "audit.html",
        candidates=candidates,
        checked_everything=not candidates,
        planned_items=len(requirements),
    )


def _confirm_audit_field(db: Session, user: User, ingredient_id: int, raw: str) -> None:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    # An empty box means "the estimate was right", which still restores
    # confidence -- that is the whole point of asking.
    text = raw.strip()
    confirm_level(db, user.id, ingredient, float(text) if text else None)


@router.post("/audit")
async def resolve_audit(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Record every answer on the page. One card at a time used to drop the rest."""
    form = await request.form()
    try:
        for key, raw in form.multi_items():
            name = str(key)
            if not name.startswith("quantity_"):
                continue
            _confirm_audit_field(db, user, int(name.removeprefix("quantity_")), str(raw))
    except (UnitConversionError, ValueError) as exc:
        today, week_end = _horizon()
        requirements = requirements_for_audit(db, user.id, today, week_end)
        candidates = build_audit(db, user.id, requirements)
        return _render(
            request,
            "audit.html",
            status_code=400,
            candidates=candidates,
            checked_everything=not candidates,
            planned_items=len(requirements),
            error=str(exc),
        )
    return RedirectResponse("/audit", status_code=303)


@router.post("/audit/{ingredient_id}")
def resolve_audit_item(
    ingredient_id: int,
    quantity: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    _confirm_audit_field(db, user, ingredient_id, quantity)
    return RedirectResponse("/audit", status_code=303)
