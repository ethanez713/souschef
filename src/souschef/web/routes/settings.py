"""Routes: settings."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.core.crypto import encrypt
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.db import get_db
from souschef.models import CanonicalIngredient, StoredCredential, User
from souschef.models.ingredient import IngredientCategory
from souschef.models.settings import CredentialKind
from souschef.web import auth
from souschef.web.common import (
    THEME_COOKIE,
    THEMES,
    _render,
    _sync_shopping,
)

router = APIRouter()


def _nyt_credential(db: Session, user: User) -> StoredCredential | None:
    return db.scalar(
        select(StoredCredential).where(
            StoredCredential.user_id == user.id,
            StoredCredential.kind == CredentialKind.NYT_COOKING_COOKIE,
        )
    )


def _settings_page(
    request: Request,
    db: Session,
    user: User,
    *,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    staples = db.scalars(
        select(CanonicalIngredient)
        .where(CanonicalIngredient.is_staple.is_(True))
        .order_by(CanonicalIngredient.name)
    ).all()

    return _render(
        request,
        "settings.html",
        status_code=status_code,
        nyt_credential=_nyt_credential(db, user),
        settings=get_settings(),
        staples=staples,
        error=error,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _settings_page(request, db, user)


@router.post("/settings/theme")
def set_theme(
    theme: str = Form(...),
    _user: User = Depends(auth.current_user),
) -> Response:
    response = RedirectResponse("/settings", status_code=303)
    if theme in THEMES:
        response.set_cookie(
            THEME_COOKIE, theme, max_age=365 * 24 * 3600, samesite="lax", httponly=True
        )
    return response


@router.post("/settings/units")
def set_units(
    unit_system: str = Form(...),
    show_alt_units: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    if unit_system in ("us", "metric"):
        user.unit_system = unit_system
    user.show_alt_units = show_alt_units in ("on", "true", "1")
    db.flush()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/staples")
def add_staple(
    request: Request,
    name: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Assume a staple. An exact catalog hit is reused; anything else is created,
    the same way naming a recipe line creates a food the catalog doesn't have.
    """
    clean = name.strip()
    if not clean:
        return _settings_page(
            request,
            db,
            user,
            error="Enter the ingredient to assume.",
            status_code=400,
        )
    match = IngredientMatcher(db).match(clean)
    if (
        match.method in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT)
        and match.ingredient_id is not None
    ):
        ingredient = db.get(CanonicalIngredient, match.ingredient_id)
        if ingredient is None:
            raise HTTPException(status_code=404, detail="No such ingredient")
    else:
        ingredient = CanonicalIngredient(
            name=clean,
            category=IngredientCategory.OTHER,
            base_unit="count",
        )
        db.add(ingredient)
        db.flush()
    ingredient.is_staple = True
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/staples/{ingredient_id}/clear")
def clear_staple(
    ingredient_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    ingredient.is_staple = False
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/{rest:path}")
def settings_form_get(
    rest: str,  # noqa: ARG001
    _user: User = Depends(auth.current_user),
) -> RedirectResponse:
    """A refresh of a settings form lands back on the page."""
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/nyt-cookie")
def save_nyt_cookie(
    request: Request,
    cookie: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Store this user's NYT session cookie, encrypted.

    Only the cookie is accepted. The password is never asked for, because NYT
    gates login behind bot detection and a stored password would be a much
    larger prize for anyone who got hold of the database.
    """
    value = cookie.strip()
    if not value:
        return _settings_page(request, db, user, error="Cookie is empty", status_code=400)

    credential = _nyt_credential(db, user)
    if credential is None:
        credential = StoredCredential(user_id=user.id, kind=CredentialKind.NYT_COOKING_COOKIE)
        db.add(credential)

    credential.ciphertext = encrypt(value)
    credential.label = f"Saved {date.today().isoformat()}"
    credential.is_working = True
    db.flush()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/nyt-cookie/delete")
def delete_nyt_cookie(
    user: User = Depends(auth.current_user), db: Session = Depends(get_db)
) -> Response:
    credential = _nyt_credential(db, user)
    if credential is not None:
        db.delete(credential)
        db.flush()
    return RedirectResponse("/settings", status_code=303)
