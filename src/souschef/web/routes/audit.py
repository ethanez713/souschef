"""Routes: audit."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from souschef.core.audit import build_audit
from souschef.db import get_db
from souschef.models import User
from souschef.planning.shopping import requirements_for_audit
from souschef.web import auth
from souschef.web.common import (
    _change_toast,
    _horizon,
    _render,
)

router = APIRouter()


def render_audit(
    request: Request,
    db: Session,
    user: User,
    *,
    changed: int = 0,
    form_error: str = "",
) -> HTMLResponse:
    """The pre-shop check: the few things worth physically verifying."""
    today, week_end = _horizon()
    requirements = requirements_for_audit(db, user.id, today, week_end)
    candidates = build_audit(db, user.id, requirements)
    return _render(
        request,
        "audit.html",
        candidates=candidates,
        change=_change_toast(db, user, changed),
        form_error=form_error,
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    changed: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return render_audit(request, db, user, changed=changed)
