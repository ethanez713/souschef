"""Routes: settings."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.core.crypto import encrypt
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.db import get_db
from souschef.models import CanonicalIngredient, Invite, StoredCredential, User
from souschef.models.ingredient import IngredientCategory
from souschef.models.settings import CredentialKind
from souschef.web import auth
from souschef.web.common import (
    THEME_COOKIE,
    THEMES,
    _is_secure_request,
    _render,
    _set_session_cookie,
    log,
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
    saved: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    invites = []
    if user.is_admin:
        invites = db.scalars(select(Invite).order_by(Invite.created_at.desc()).limit(20)).all()

    staples = db.scalars(
        select(CanonicalIngredient)
        .where(CanonicalIngredient.is_staple.is_(True))
        .order_by(CanonicalIngredient.name)
    ).all()

    # Shown once, from the short-lived cookie set at creation. The plaintext is
    # never stored, and it is not put on the URL.
    new_invite = auth.read_invite_flash(request.cookies.get(auth.FLASH_COOKIE)) or ""
    response = _render(
        request,
        "settings.html",
        status_code=status_code,
        nyt_credential=_nyt_credential(db, user),
        settings=get_settings(),
        invites=invites,
        staples=staples,
        new_invite=new_invite,
        error=error,
        saved=saved,
        now=datetime.now(UTC),
    )
    if new_invite:
        response.delete_cookie(
            auth.FLASH_COOKIE,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
    return response


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _settings_page(request, db, user, saved=saved if saved == "password" else "")


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
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/staples/{ingredient_id}/clear")
def clear_staple(
    ingredient_id: int,
    _user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    ingredient.is_staple = False
    db.flush()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/password")
def change_password(
    request: Request,
    current: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    if not auth.verify_password(user.password_hash, current):
        return _settings_page(
            request, db, user, error="Current password is incorrect", status_code=400
        )
    if password != confirm:
        return _settings_page(
            request, db, user, error="New passwords do not match", status_code=400
        )
    try:
        auth.set_password(db, user, password)
    except ValueError as exc:
        return _settings_page(request, db, user, error=str(exc), status_code=400)

    # set_password bumped the session epoch, which just invalidated this
    # request's own cookie along with every other one. Re-issue so the person
    # who changed their password isn't the only one signed out by it.
    response = RedirectResponse("/settings?saved=password", status_code=303)
    _set_session_cookie(request, response, user)
    return response


@router.post("/settings/invites")
def create_invite_route(
    request: Request,
    note: str = Form(""),
    user: User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> Response:
    _, code = auth.create_invite(db, user, note=note)
    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(
        auth.FLASH_COOKIE,
        auth.issue_invite_flash(code),
        max_age=auth.FLASH_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_secure_request(request),
    )
    return response


@router.post("/settings/invites/{invite_id}/revoke")
def revoke_invite(
    invite_id: int,
    user: User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> Response:
    invite = db.get(Invite, invite_id)
    if invite is not None and invite.redeemed_at is None:
        db.delete(invite)
        db.flush()
        log.info("Invite %d revoked by %r", invite_id, user.username)
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/{rest:path}")
def settings_form_get(
    rest: str,  # noqa: ARG001
    _user: User = Depends(auth.current_user),
) -> RedirectResponse:
    """A refresh of a settings form (password, staple, invite) lands back here."""
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
