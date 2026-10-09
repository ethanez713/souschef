"""Routes: auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.db import get_db
from souschef.models import User
from souschef.web import auth
from souschef.web.common import (
    _render,
    _safe_next,
    _set_session_cookie,
)

router = APIRouter()


def _optional_user(request: Request, db: Session) -> User | None:
    """The signed-in account, or None. Never redirects."""
    if get_settings().single_user:
        request.state.user = auth.sole_user(db)
        return request.state.user
    parsed = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    if parsed is None:
        return None
    user_id, epoch = parsed
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.session_epoch != epoch:
        return None
    request.state.user = user
    return user


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)) -> Response:
    if get_settings().single_user:
        return RedirectResponse("/", status_code=303)
    # An empty database means nobody has claimed this instance yet. Whoever
    # reaches it first becomes the admin.
    if auth.user_count(db) == 0:
        return _render(request, "setup.html")
    nxt = request.query_params.get("next", "")
    safe = _safe_next(nxt)
    shown = "" if safe == "/" and nxt.strip() in {"", "/"} else safe
    return _render(request, "login.html", next=shown)


@router.get("/setup")
def setup_get() -> RedirectResponse:
    """A refresh after a failed claim is a GET. The form lives on /login."""
    return RedirectResponse("/login", status_code=303)


@router.post("/setup")
def setup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Claim an unclaimed instance, creating the admin account."""
    if auth.user_count(db) > 0:
        raise HTTPException(status_code=400, detail="Already set up")
    if password != confirm:
        return _render(request, "setup.html", error="Passwords do not match.", username=username)
    try:
        user = auth.create_user(db, username, password, is_admin=True)
    except ValueError as exc:
        return _render(request, "setup.html", error=str(exc), username=username)

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, user)
    return response


@router.get("/join", response_class=HTMLResponse)
def join_form(request: Request, code: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    """Redeem an invite. The code is the only thing gating this page."""
    if auth.user_count(db) == 0:
        return RedirectResponse("/login", status_code=303)
    if _optional_user(request, db) is not None:
        return _render(
            request,
            "join.html",
            code=code,
            invite_ok=False,
            already=True,
            error=None,
        )
    invite = auth.find_usable_invite(db, code) if code else None
    return _render(
        request,
        "join.html",
        code=code,
        invite_ok=invite is not None,
        already=False,
        error=None if invite or not code else "That invite is invalid, already used, or expired.",
    )


@router.post("/join")
def join(
    request: Request,
    code: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    if password != confirm:
        return _render(
            request,
            "join.html",
            code=code,
            invite_ok=True,
            username=username,
            error="Passwords do not match.",
        )
    try:
        user = auth.redeem_invite(db, code, username, password)
    except ValueError as exc:
        return _render(
            request,
            "join.html",
            code=code,
            invite_ok=auth.find_usable_invite(db, code) is not None,
            username=username,
            error=str(exc),
        )

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, user)
    return response


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),  # noqa: A002 - the form field is the return path
    db: Session = Depends(get_db),
) -> Response:
    destination = _safe_next(next)
    shown_next = "" if destination == "/" and not next.strip() else destination
    ip = auth.client_key(request)
    if auth.is_locked_out(ip, username):
        return _render(
            request,
            "login.html",
            username=username,
            next=shown_next,
            error="Too many attempts. Try again in a few minutes.",
        )

    user = auth.authenticate(db, username, password)
    if user is None:
        auth.record_failure(ip, username)
        # One message for both "no such user" and "wrong password": telling
        # them apart is a free list of who has an account here.
        return _render(
            request,
            "login.html",
            username=username,
            next=shown_next,
            error="Incorrect username or password.",
        )

    auth.clear_failures(ip, username)
    response = RedirectResponse(destination, status_code=303)
    _set_session_cookie(request, response, user)
    return response


@router.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response
