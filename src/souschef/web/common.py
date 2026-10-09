"""Helpers shared by more than one router."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from souschef import logconfig
from souschef.core.units import UnitConversionError, dimension_of, humanize, us_equivalent
from souschef.models import CanonicalIngredient, EventReason, InventoryEvent, Recipe, User
from souschef.models.base import Base
from souschef.planning.shopping import build_shopping_list
from souschef.web import auth

PLAN_DAYS = 10


# Uncooked meals stay visible this long, and a cook can be logged back this far.
PAST_DAYS = 7


WEB_DIR = Path(__file__).parent


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _measure(quantity: float, ingredient: CanonicalIngredient, round_count: bool = False) -> str:
    """A quantity the way it should read, without touching the stored number."""
    shown = float(quantity)
    if round_count and ingredient.base_unit == "count":
        shown = float(round(shown))
    text = humanize(shown, ingredient.base_unit)
    if ingredient.name.casefold() == "garlic" and ingredient.base_unit == "count":
        word = "clove" if abs(shown - 1) < 0.05 else "cloves"
        return f"{text} {word}"
    return text


def _stock(quantity: float, ingredient: CanonicalIngredient) -> str:
    """Pantry levels. A remainder below one base unit reads as out, not "0.4 g"."""
    if 0 < quantity < 1:
        return "out"
    return _measure(quantity, ingredient)


def _unit_label(ingredient: CanonicalIngredient) -> str:
    if ingredient.name.casefold() == "garlic" and ingredient.base_unit == "count":
        return "cloves"
    return ingredient.base_unit


def _receipt_label(status: object) -> str:
    key = getattr(status, "value", status)
    return {
        "uploaded": "Uploaded",
        "ocr_running": "Scanning",
        "needs_review": "Needs review",
        "applied": "Added to the pantry",
        "failed": "Couldn't scan",
    }.get(str(key), str(key).replace("_", " "))


templates.env.filters["humanize"] = humanize


templates.env.filters["us_equivalent"] = us_equivalent


templates.env.filters["measure"] = _measure


templates.env.filters["stock"] = _stock


templates.env.filters["unit_label"] = _unit_label


templates.env.filters["receipt_label"] = _receipt_label


logconfig.configure()


log = logconfig.get_logger("souschef.web")


THEME_COOKIE = "theme"


THEMES = ("light", "dark", "auto")


def _theme(request: Request) -> str:
    chosen = request.cookies.get(THEME_COOKIE, "")
    return chosen if chosen in THEMES else "light"


def _render(request: Request, template: str, *, status_code: int = 200, **context) -> HTMLResponse:
    # `user` is injected rather than passed by each route: the nav and sign-out
    # control need it on every authenticated page, and a route that forgot it
    # would silently render the signed-out chrome to a signed-in user.
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("theme", _theme(request))
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _safe_next(value: str | None) -> str:
    """A same-origin path to return to after sign-in.

    The share target's query string contains a full URL, so ``://`` is only
    rejected in the path. An absolute or scheme-relative value is dropped.
    """
    if not value:
        return "/"
    value = value.strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or value.startswith("/\\")
        or "\\" in value
        or "\r" in value
        or "\n" in value
        or "\x00" in value
    ):
        return "/"
    path = value.split("?", 1)[0].split("#", 1)[0]
    if "://" in path:
        return "/"
    return value


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _kitchen_unit_message(exc: UnitConversionError, unit: str) -> str:
    """The bag message, for a count-word we also can't weigh (a bunch, a head)."""
    text = str(exc)
    if "Don't know how much" in text:
        return text
    if "per-item weight" in text:
        word = unit.strip().lower().rstrip(".") or "of that"
        return f"Don't know how much one {word} weighs. Enter grams, ounces, pounds, or a count."
    return text


def _on_hand(view) -> bool:  # noqa: ANN001
    return not view.is_staple and view.quantity >= 1


def _horizon() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=PLAN_DAYS - 1)


def _sync_shopping(db: Session, user_id: int):  # noqa: ANN202
    start, end = _horizon()
    return build_shopping_list(db, user_id, covers_from=start, covers_to=end)


def _owned(db: Session, model: type[Base], obj_id: int, user: User, what: str):  # noqa: ANN201
    """Fetch a user-owned row by id, or 404.

    "Belongs to someone else" and "does not exist" deliberately return the same
    404. Distinguishing them would let one user probe which ids are real in
    another user's kitchen, and there is nothing useful to do with the
    difference anyway.
    """
    obj = db.get(model, obj_id)
    if obj is None or obj.user_id != user.id:
        raise HTTPException(status_code=404, detail=f"No such {what}")
    return obj


def _is_secure_request(request: Request) -> bool:
    """Whether the *browser* reached us over HTTPS.

    A local run is plain HTTP, so the session cookie must not be marked Secure:
    the browser would never send it back and sign-in would appear to succeed
    while every following request came in anonymous. Behind a TLS tunnel the app
    still sees http and has to take the forwarded header's word for it.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


def _set_session_cookie(request: Request, response: Response, user: User) -> None:
    # Issuing a session *is* a sign-in, so record it here rather than only in
    # the login route -- otherwise an account created via /setup or /join reads
    # as "last login: never" while plainly being signed in.
    user.last_login_at = datetime.now(UTC)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(user),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_secure_request(request),
    )


def _discard_notice(db: Session, user: User, event_id: int) -> dict | None:
    """What the undo notice needs: the discarded item's name and the event Undo reverses."""
    if not event_id:
        return None
    event = db.get(InventoryEvent, event_id)
    if event is None or event.user_id != user.id or event.reason != EventReason.WASTE:
        return None
    ingredient = db.get(CanonicalIngredient, event.ingredient_id)
    return {"event_id": event.id, "name": ingredient.name if ingredient else "item"}


def _adhoc_units(unit: str) -> tuple[str, str | None]:
    """Base unit and the (validated) input unit for a user-typed pantry item.

    An unrecognised unit is dropped rather than stored, so a typo cannot make the
    level unconvertible; the amount is then counted as-is.
    """
    cleaned = unit.strip()
    if not cleaned:
        return "count", None
    try:
        dim = dimension_of(cleaned)
    except UnitConversionError as exc:
        # "bag" is a real pint unit (a sack of cement). Swallowing that error
        # would store the number and hide the mistake; an unknown word can
        # still be counted as-is.
        if "Don't know how much" in str(exc):
            raise
        return "count", None
    return {"mass": "g", "volume": "ml"}.get(dim, "count"), cleaned


MAX_SERVINGS = 50


def _default_servings(recipe: Recipe) -> int:
    return recipe.last_servings or recipe.servings or 4
