"""FastAPI application: routes, templates, and the PWA shell."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, quote_plus, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from souschef import logconfig
from souschef.config import ensure_local_secrets, get_settings
from souschef.core.audit import build_audit
from souschef.core.crypto import encrypt
from souschef.core.inventory import (
    confirm_level,
    consume_for_recipe,
    cover_cook_shortfall,
    discard_item,
    pantry_view,
    record_event,
    set_level,
    undo_discard,
)
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.recipes import recipe_requirements, save_imported_recipe
from souschef.core.spoilage import Freshness, freshness_by_ingredient
from souschef.core.units import UnitConversionError, dimension_of, humanize, us_equivalent
from souschef.db import get_db
from souschef.ingest.receipt_service import (
    ReceiptError,
    apply_receipt,
    create_receipt,
    image_key,
    scan_receipt,
)
from souschef.ingest.recipes.importers import ImportError_, import_from_url
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    Invite,
    MealPlanEntry,
    MealSlot,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
    Recipe,
    ShoppingListItem,
    StoredCredential,
    User,
)
from souschef.models.base import Base
from souschef.models.ingredient import AliasSource, IngredientCategory
from souschef.models.settings import CredentialKind
from souschef.planning.shopping import (
    ShoppingError,
    add_manual_item,
    build_shopping_list,
    ingredients_stocked_since,
    live_list,
    requirements_for_audit,
    stock_ticked_items,
    unnamed_recipe_lines,
)
from souschef.planning.suggest import Surface, by_surface, suggest
from souschef.storage import StorageError, get_storage
from souschef.web import auth

PLAN_DAYS = 10
# Uncooked meals stay visible this long, and a cook can be logged back this far.
PAST_DAYS = 7

LIST_STOCK_NOTE = "Already added from the shopping list, so this line stays unticked."

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


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN201
    ensure_local_secrets()
    logconfig.log_startup(get_settings())
    yield
    log.info("SousChef shutting down")


# openapi_url is disabled alongside the docs UIs: with only those two off,
# /openapi.json still served the full route schema to anonymous callers.
app = FastAPI(
    title="SousChef", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)
app.mount("/static", StaticFiles(directory=str(WEB_DIR.parent / "static")), name="static")

# Requests slower than this are logged as a warning. Everything here is a
# handful of SQLite reads except receipt OCR, so a slow page means something
# is wrong rather than merely busy.
SLOW_REQUEST_SECONDS = 3.0


@app.middleware("http")
async def request_logging(request: Request, call_next):  # noqa: ANN001, ANN201
    """Log every request, and make unhandled errors impossible to miss.

    Without this an exception surfaces as a bare 500 in the browser and nothing
    anywhere else. The id is echoed in a response header so a reported failure
    can be found in the log.
    """
    request_id = logconfig.new_request_id()
    token = logconfig.set_request_id(request_id)
    started = time.perf_counter()
    route = f"{request.method} {request.url.path}"

    # The reset has to come after the summary line is emitted, not in a finally
    # that runs before it -- otherwise every request log line is tagged "-" and
    # cannot be correlated with the warnings raised while handling it, which is
    # the entire reason for having an id.
    try:
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            # exc_info, so the traceback lands in the log, not just the message.
            log.exception("%s failed after %.0fms", route, elapsed)
            raise

        elapsed = (time.perf_counter() - started) * 1000

        if response.status_code >= 500:
            log.error("%s -> %d (%.0fms)", route, response.status_code, elapsed)
        elif response.status_code >= 400:
            log.warning("%s -> %d (%.0fms)", route, response.status_code, elapsed)
        elif elapsed > SLOW_REQUEST_SECONDS * 1000:
            log.warning("%s -> %d SLOW (%.0fms)", route, response.status_code, elapsed)
        else:
            log.info("%s -> %d (%.0fms)", route, response.status_code, elapsed)

        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logconfig.reset_request_id(token)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Everything is served from our own origin; no CDNs, so this can be strict.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    return response


THEME_COOKIE = "theme"
THEMES = ("light", "dark", "auto")


def _theme(request: Request) -> str:
    chosen = request.cookies.get(THEME_COOKIE, "")
    return chosen if chosen in THEMES else "light"


def _render(
    request: Request, template: str, *, status_code: int = 200, **context
) -> HTMLResponse:
    # `user` is injected rather than passed by each route: the nav and sign-out
    # control need it on every authenticated page, and a route that forgot it
    # would silently render the signed-out chrome to a signed-in user.
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("theme", _theme(request))
    return templates.TemplateResponse(
        request, template, context, status_code=status_code
    )


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.exception_handler(StarletteHTTPException)
async def http_exception_page(request: Request, exc: StarletteHTTPException) -> Response:
    """A browser gets a page. A client that asked only for JSON still gets JSON.

    Routing 404s and 405s raise Starlette's HTTPException, not FastAPI's subclass,
    so the handler has to be registered on the base class or they stay raw JSON.
    Redirects (the signed-out 303) must keep their Location header.
    """
    if exc.status_code in {301, 302, 303, 307, 308}:
        return await http_exception_handler(request, exc)
    if exc.headers and any(key.lower() == "location" for key in exc.headers):
        return await http_exception_handler(request, exc)
    if _wants_json(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    titles = {
        404: "Not found",
        403: "Not allowed",
        400: "That didn't work",
        405: "That didn't work",
    }
    return _render(
        request,
        "error.html",
        status_code=exc.status_code,
        title=titles.get(exc.status_code, "Something went wrong"),
        detail=detail,
    )


@app.exception_handler(RequestValidationError)
async def validation_page(request: Request, exc: RequestValidationError) -> Response:
    if _wants_json(request):
        return await request_validation_exception_handler(request, exc)
    return _render(
        request,
        "error.html",
        status_code=422,
        title="That didn't work",
        detail="The form was incomplete or invalid.",
    )


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


def _kitchen_unit_message(exc: UnitConversionError, unit: str) -> str:
    """The bag message, for a count-word we also can't weigh (a bunch, a head)."""
    text = str(exc)
    if "Don't know how much" in text:
        return text
    if "per-item weight" in text:
        word = unit.strip().lower().rstrip(".") or "of that"
        return (
            f"Don't know how much one {word} weighs. "
            "Enter grams, ounces, pounds, or a count."
        )
    return text


def _on_hand(view) -> bool:  # noqa: ANN001
    return not view.is_staple and view.quantity >= 1


def _horizon() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=PLAN_DAYS - 1)


def _cook_dates(today: date | None = None) -> list[date]:
    """Today, then back through the days a missed cook can still be logged."""
    today = today or date.today()
    return [today - timedelta(days=offset) for offset in range(PAST_DAYS + 1)]


def _parse_cook_date(raw: str, today: date | None = None) -> date | None:
    text = raw.strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    if day not in set(_cook_dates(today)):
        return None
    return day


def _cook_when(cooked_on: date) -> datetime:
    if cooked_on >= date.today():
        return datetime.now(UTC)
    noon = datetime.min.time().replace(hour=12)
    return datetime.combine(cooked_on, noon, tzinfo=UTC)


def _open_plan_entry(db: Session, user_id: int, recipe_id: int, day: date) -> MealPlanEntry | None:
    """That day's still-open plan entry for this recipe, if the cook should close it."""
    return db.scalar(
        select(MealPlanEntry)
        .where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.recipe_id == recipe_id,
            MealPlanEntry.plan_date == day,
            MealPlanEntry.cooked_at.is_(None),
            MealPlanEntry.skipped_at.is_(None),
        )
        .order_by(MealPlanEntry.id)
    )


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


@app.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request, db: Session = Depends(get_db)
) -> Response:
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


@app.get("/setup")
def setup_get() -> RedirectResponse:
    """A refresh after a failed claim is a GET. The form lives on /login."""
    return RedirectResponse("/login", status_code=303)


@app.post("/setup")
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


@app.get("/join", response_class=HTMLResponse)
def join_form(request: Request, code: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    """Redeem an invite. The code is the only thing gating this page."""
    if auth.user_count(db) == 0:
        return RedirectResponse("/login", status_code=303)
    if _optional_user(request, db) is not None:
        return _render(
            request, "join.html", code=code, invite_ok=False, already=True, error=None,
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


@app.post("/join")
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
            request, "join.html", code=code, invite_ok=True,
            username=username, error="Passwords do not match.",
        )
    try:
        user = auth.redeem_invite(db, code, username, password)
    except ValueError as exc:
        return _render(
            request, "join.html", code=code,
            invite_ok=auth.find_usable_invite(db, code) is not None,
            username=username, error=str(exc),
        )

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response, user)
    return response


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


@app.post("/login")
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
            request, "login.html", username=username, next=shown_next,
            error="Too many attempts. Try again in a few minutes.",
        )

    user = auth.authenticate(db, username, password)
    if user is None:
        auth.record_failure(ip, username)
        # One message for both "no such user" and "wrong password": telling
        # them apart is a free list of who has an account here.
        return _render(
            request, "login.html", username=username, next=shown_next,
            error="Incorrect username or password.",
        )

    auth.clear_failures(ip, username)
    response = RedirectResponse(destination, status_code=303)
    _set_session_cookie(request, response, user)
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    wasted: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    suggestions = suggest(db, user.id, limit=12)
    grouped = by_surface(suggestions)

    today = date.today()
    past_start = today - timedelta(days=PAST_DAYS)
    past_meals = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= past_start,
            MealPlanEntry.plan_date < today,
            MealPlanEntry.cooked_at.is_(None),
            MealPlanEntry.skipped_at.is_(None),
            MealPlanEntry.recipe_id.is_not(None),
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()
    upcoming = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= today,
            MealPlanEntry.cooked_at.is_(None),
        )
        .order_by(MealPlanEntry.plan_date)
        .limit(7)
    ).all()

    pantry = pantry_view(db, user.id)
    freshness = freshness_by_ingredient(db, user.id, pantry, today)
    use_soon = [
        v for v in pantry if freshness.get(v.ingredient.id) == Freshness.USE_SOON
    ]
    probably_gone = [
        v for v in pantry if freshness.get(v.ingredient.id) == Freshness.PROBABLY_GONE
    ]
    stale = sorted(
        (v for v in pantry if v.confidence < 0.5 and _on_hand(v)),
        key=lambda v: v.confidence,
    )[:5]

    return _render(
        request,
        "dashboard.html",
        cook_now=grouped[Surface.COOK_NOW][:10],
        nearly=grouped[Surface.NEARLY_THERE][:10],
        bigger_shop=grouped[Surface.REACH][:10],
        past_meals=past_meals,
        upcoming=upcoming,
        today=today,
        freshness=freshness,
        use_soon=use_soon[:8],
        probably_gone=probably_gone[:8],
        stale=stale,
        discarded=_discard_notice(db, user, wasted),
        pantry_count=len(pantry),
        recipe_count=db.scalar(
            select(func.count()).select_from(Recipe).where(Recipe.user_id == user.id)
        )
        or 0,
    )


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


def _discard_notice(db: Session, user: User, event_id: int) -> dict | None:
    """What the undo notice needs: the discarded item's name and the event Undo reverses."""
    if not event_id:
        return None
    event = db.get(InventoryEvent, event_id)
    if event is None or event.user_id != user.id or event.reason != EventReason.WASTE:
        return None
    ingredient = db.get(CanonicalIngredient, event.ingredient_id)
    return {"event_id": event.id, "name": ingredient.name if ingredient else "item"}


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


@app.get("/pantry", response_class=HTMLResponse)
def pantry_page(
    request: Request,
    q: str = "",
    wasted: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _pantry_page(request, db, user, q, wasted=wasted)


@app.post("/pantry/{ingredient_id}/level")
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


@app.post("/pantry/{ingredient_id}/confirm")
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


@app.post("/pantry/{ingredient_id}/discard")
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


@app.post("/pantry/undo/{event_id}")
def undo_pantry_discard(
    event_id: int,
    q: str = Form(""),
    next_path: str = Form("", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    undo_discard(db, user.id, event_id)
    return RedirectResponse(_after_pantry_edit(q, next_path), status_code=303)


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


@app.post("/pantry/add")
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
            request, db, user, q,
            add_open=True, add_error=message, add_name=name, add_quantity=str(quantity),
            add_unit=unit, status_code=400,
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
            db, user.id, ingredient, reason=EventReason.PURCHASE,
            delta=quantity, unit=store_unit,
        )
    except UnitConversionError as exc:
        return reject(_kitchen_unit_message(exc, unit))
    return RedirectResponse(_pantry_url(q), status_code=303)


@app.get("/pantry/add")
def add_to_pantry_get(_user: User = Depends(auth.current_user)) -> RedirectResponse:
    """A refresh after a failed add is a GET. Send it back to the form."""
    return RedirectResponse("/pantry", status_code=303)


@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(
    request: Request,
    q: str = "",
    import_url: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    suggestions = suggest(db, user.id, limit=200)
    needle = q.strip().casefold()
    if needle:
        suggestions = [s for s in suggestions if _recipe_matches(s.recipe, needle)]
    template = "recipes_list.html" if _is_htmx(request) else "recipes.html"
    return _render(
        request,
        template,
        grouped=by_surface(suggestions),
        query=q,
        import_url=import_url if import_url.startswith(("http://", "https://")) else "",
        # Open the importer when a share landed here, or when there is nothing else to do.
        import_open=bool(
            import_url or request.query_params.get("import") or (not suggestions and not needle)
        ),
    )


def _recipe_matches(recipe: Recipe, needle: str) -> bool:
    if needle in recipe.title.casefold() or needle in (recipe.author or "").casefold():
        return True
    for line in recipe.ingredients:
        if needle in line.raw_text.casefold():
            return True
        if line.ingredient is not None and needle in line.ingredient.name.casefold():
            return True
    return False


def _recipe_rows(recipe: Recipe, requirements) -> tuple[list[dict], int]:  # noqa: ANN001
    """Matched and unidentified lines in the recipe's own order."""
    pools: dict[str, list] = {}
    for req in requirements.requirements:
        pools.setdefault(req.raw_text, []).append(req)
    rows: list[dict] = []
    unmatched = 0
    for line in recipe.ingredients:
        bucket = pools.get(line.raw_text)
        if bucket:
            rows.append({"req": bucket.pop(0), "raw": line.raw_text})
        else:
            rows.append({"req": None, "raw": line.raw_text})
            unmatched += 1
    for bucket in pools.values():
        rows.extend({"req": req, "raw": req.raw_text} for req in bucket)
    return rows, unmatched


def _recipe_page(
    request: Request,
    db: Session,
    user: User,
    recipe: Recipe,
    *,
    cooked: bool = False,
    notice: str = "",
    match_error: str = "",
    saved: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    requirements = recipe_requirements(db, recipe)
    rows, unmatched = _recipe_rows(recipe, requirements)
    pantry = {v.ingredient.id: v for v in pantry_view(db, user.id)}
    today, _ = _horizon()
    catalog = db.scalars(
        select(CanonicalIngredient.name).order_by(CanonicalIngredient.name)
    ).all()
    return _render(
        request,
        "recipe_detail.html",
        status_code=status_code,
        recipe=recipe,
        rows=rows,
        unmatched=unmatched,
        catalog=catalog,
        pantry=pantry,
        cooked=cooked,
        notice=notice,
        match_error=match_error,
        saved=saved,
        today=today,
        days=[today + timedelta(days=offset) for offset in range(PLAN_DAYS)],
    )


@app.get("/recipes/{recipe_id}", response_class=HTMLResponse)
def recipe_detail(
    request: Request,
    recipe_id: int,
    cooked: int = 0,
    notice: str = "",
    saved: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    return _recipe_page(
        request, db, user, recipe,
        cooked=bool(cooked), notice=notice, saved=saved.strip()[:80],
    )


@app.post("/recipes/import")
def import_recipe(
    request: Request,
    url: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        result = import_from_url(db, user.id, url)
    except ImportError_ as exc:
        return _render(request, "import_result.html", error=str(exc), url=url)

    recipe = save_imported_recipe(
        db, user.id, result.recipe, source=result.source, external_id=result.external_id
    )
    location = f"/recipes/{recipe.id}"
    if result.notice:
        location += "?notice=" + quote(result.notice)
    return RedirectResponse(location, status_code=303)


@app.post("/recipes/{recipe_id}/match")
def correct_recipe_match(
    request: Request,
    recipe_id: int,
    raw_text: str = Form(...),
    name: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Point a recipe line at the food the cook named, and remember it.

    Exact catalog hits are reused. Anything else is stored under the name they
    typed — a fuzzy guess is how mirin became steak. The alias is the parsed
    ingredient name, not the whole line, so "2 tbsp mirin" does not become the key.
    """
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    clean = name.strip()
    if not clean:
        return _recipe_page(
            request, db, user, recipe,
            match_error="Enter the ingredient this line actually is.",
            status_code=400,
        )

    lines = [line for line in recipe.ingredients if line.raw_text == raw_text]
    if not lines:
        raise HTTPException(status_code=404, detail="No such line")

    from souschef.ingest.recipes.parsing import parse_line

    matcher = IngredientMatcher(db)
    match = matcher.match(clean)
    try:
        if match.method in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT):
            ingredient = db.get(CanonicalIngredient, match.ingredient_id)
        else:
            base_unit, _store_unit = _adhoc_units(lines[0].unit or "")
            ingredient = CanonicalIngredient(
                name=clean, category=IngredientCategory.OTHER, base_unit=base_unit
            )
            db.add(ingredient)
            db.flush()
    except UnitConversionError as exc:
        return _recipe_page(
            request, db, user, recipe,
            match_error=_kitchen_unit_message(exc, lines[0].unit or ""),
            status_code=400,
        )
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")

    parsed_name = parse_line(raw_text).name.strip()
    if parsed_name and parsed_name.casefold() != raw_text.strip().casefold():
        matcher.learn(parsed_name, ingredient.id, source=AliasSource.MANUAL, is_receipt=False)
    for line in lines:
        line.ingredient_id = ingredient.id
        line.match_confidence = 1.0
    db.flush()
    return RedirectResponse(
        f"/recipes/{recipe.id}?saved={quote(clean)}", status_code=303
    )


MAX_SERVINGS = 50


def _default_servings(recipe: Recipe) -> int:
    return recipe.last_servings or recipe.servings or 4


def _cook_page(
    request: Request,
    db: Session,
    user: User,
    recipe: Recipe,
    *,
    plan_entry: MealPlanEntry | None = None,
    servings: int = 0,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    servings = (
        servings or (plan_entry.servings if plan_entry else 0) or _default_servings(recipe)
    )
    servings = min(servings, MAX_SERVINGS)
    requirements = recipe_requirements(db, recipe, servings=servings)
    pantry = {v.ingredient.id: v for v in pantry_view(db, user.id)}

    debits: list[dict] = []
    by_id: dict[int, dict] = {}
    for req in requirements.requirements:
        if req.quantity <= 0 or req.is_staple:
            continue
        row = by_id.get(req.ingredient.id)
        if row is None:
            have = pantry[req.ingredient.id].quantity if req.ingredient.id in pantry else 0.0
            row = {"ingredient": req.ingredient, "quantity": 0.0, "have": have}
            by_id[req.ingredient.id] = row
            debits.append(row)
        row["quantity"] += req.quantity
    for row in debits:
        row["over"] = row["quantity"] > row["have"] + 1e-6

    today = date.today()
    cooked_on = today
    if plan_entry is not None and plan_entry.plan_date in set(_cook_dates(today)):
        cooked_on = plan_entry.plan_date
    return _render(
        request,
        "cook_preview.html",
        status_code=status_code,
        recipe=recipe,
        debits=debits,
        not_tracked=list(requirements.unmatched),
        servings=servings,
        entry=plan_entry,
        cook_dates=_cook_dates(today),
        cooked_on=cooked_on,
        today=today,
        error=error,
    )


@app.get("/recipes/{recipe_id}/cook", response_class=HTMLResponse)
def cook_preview(
    request: Request,
    recipe_id: int,
    entry: int = 0,
    servings: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    plan_entry = None
    if entry:
        plan_entry = _owned(db, MealPlanEntry, entry, user, "plan entry")
        if plan_entry.recipe_id != recipe.id:
            raise HTTPException(status_code=404, detail="No such plan entry")
    return _cook_page(request, db, user, recipe, plan_entry=plan_entry, servings=servings)


@app.post("/recipes/{recipe_id}/cooked")
async def mark_cooked(
    request: Request,
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    form = await request.form()
    servings = min(int(form.get("servings") or 0), MAX_SERVINGS)
    entry_id = int(form.get("entry_id") or 0)
    raw_date = str(form.get("cooked_on") or "")
    cooked_on = _parse_cook_date(raw_date)
    plan_entry = None
    if entry_id:
        plan_entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
        if plan_entry.recipe_id != recipe.id:
            raise HTTPException(status_code=404, detail="No such plan entry")
    if raw_date.strip() and cooked_on is None:
        return _cook_page(
            request, db, user, recipe,
            plan_entry=plan_entry, servings=servings,
            error="Pick a day from today back to a week ago.",
            status_code=400,
        )
    if cooked_on is None:
        cooked_on = date.today()

    proposed: dict[int, float] = {}
    used: dict[int, float] = {}
    for key, raw in form.items():
        value = str(raw).strip()
        if not value:
            continue
        if key.startswith("proposed_"):
            proposed[int(key.removeprefix("proposed_"))] = float(value)
        elif key.startswith("qty_"):
            used[int(key.removeprefix("qty_"))] = float(value)

    if not used:
        used = recipe_requirements(db, recipe, servings=servings or None).as_map()

    notes = {
        iid: f"cook adjust: proposed {proposed[iid]:g}, used {amount:g}"
        for iid, amount in used.items()
        if iid in proposed and abs(amount - proposed[iid]) > 1e-6
    }
    when = _cook_when(cooked_on)
    # The purchase has to land before the debit so the ledger's running total
    # never pretends the ingredient was used from an empty jar.
    cover_cook_shortfall(db, user.id, used, recipe_id=recipe.id, occurred_at=when)
    consume_for_recipe(
        db, user.id, used, recipe_id=recipe.id, notes=notes or None, occurred_at=when
    )

    recipe.times_cooked += 1
    previous = recipe.last_cooked_at
    if previous is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if previous is None or previous < when:
        recipe.last_cooked_at = when

    # The day on the form decides which plan entry closes, so logging Thursday
    # from the recipe page still attaches to Thursday.
    attached = _open_plan_entry(db, user.id, recipe.id, cooked_on)
    if attached is not None:
        attached.cooked_at = when
        if servings:
            attached.servings = servings
    if servings:
        recipe.last_servings = servings
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}?cooked=1", status_code=303)


_RECIPE_GROUPS = (
    (Surface.COOK_NOW, "Ready to cook"),
    (Surface.NEARLY_THERE, "Missing a few things"),
    (Surface.REACH, "Other recipes"),
)


def _recipe_groups(
    db: Session, user_id: int, entries: list[MealPlanEntry], today: date
) -> list[tuple[str, list[tuple[Recipe, str]]]]:
    """Every recipe for the day pickers, grouped by how cookable it is now."""
    planned = _planned_days(entries, today)
    grouped = by_surface(suggest(db, user_id, limit=100_000))
    return [
        (
            label,
            [
                (s.recipe, f" · planned {planned[s.recipe.id]}" if s.recipe.id in planned else "")
                for s in grouped[surface]
            ],
        )
        for surface, label in _RECIPE_GROUPS
        if grouped[surface]
    ]


def _planned_days(entries: list[MealPlanEntry], today: date) -> dict[int, str]:
    """Upcoming days each recipe is already on, for the "planned Thu" marker."""
    planned: dict[int, list[date]] = {}
    for entry in entries:
        if (
            entry.recipe_id is not None
            and entry.plan_date >= today
            and entry.cooked_at is None
            and entry.skipped_at is None
        ):
            planned.setdefault(entry.recipe_id, []).append(entry.plan_date)
    return {
        recipe_id: ", ".join(day.strftime("%a") for day in days)
        for recipe_id, days in planned.items()
    }


@app.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    skipped: int = 0,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    today, week_end = _horizon()
    past_start = today - timedelta(days=PAST_DAYS)
    entries = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= past_start,
            MealPlanEntry.plan_date <= week_end,
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()

    days = [today + timedelta(days=offset) for offset in range(PLAN_DAYS)]
    span = (week_end - past_start).days + 1
    shown = [past_start + timedelta(days=offset) for offset in range(span)]
    by_day = {day: [e for e in entries if e.plan_date == day] for day in shown}

    return _render(
        request,
        "plan.html",
        by_day=by_day,
        days=days,
        today=today,
        has_meals=any(by_day.values()),
        past_days=[day for day in shown if day < today and by_day[day]],
        recipe_groups=_recipe_groups(db, user.id, entries, today),
        week_end=week_end,
        skipped=bool(skipped),
    )


def _plan_recipe(db: Session, user_id: int, recipe_id: int, day: date, servings: int) -> bool:
    """Plan a recipe on a day. False when it is already there.

    The same recipe twice on one day doubled every ingredient on the list with
    no signal that it had happened.
    """
    already = db.scalar(
        select(MealPlanEntry.id).where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.recipe_id == recipe_id,
            MealPlanEntry.plan_date == day,
        )
    )
    if already is not None:
        return False
    db.add(
        MealPlanEntry(
            user_id=user_id,
            plan_date=day,
            slot=MealSlot.DINNER,
            recipe_id=recipe_id,
            servings=servings,
        )
    )
    return True


@app.post("/plan/add")
async def add_to_plan(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    recipe_id = int(form.get("recipe_id") or 0)
    # Validate the recipe is theirs before planning it: without this, posting an
    # arbitrary id would put another user's recipe on your calendar, and the
    # plan page would then render its title.
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    servings = min(int(form.get("servings") or 0), MAX_SERVINGS) or _default_servings(recipe)
    skipped = False
    added = False
    for raw in form.getlist("plan_date"):
        value = str(raw).strip()
        if not value:
            continue
        if _plan_recipe(db, user.id, recipe_id, date.fromisoformat(value), servings):
            added = True
        else:
            skipped = True
    if added:
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse("/plan?skipped=1" if skipped else "/plan", status_code=303)


NOTE_TITLES = ("Eating out", "Leftovers")


@app.post("/plan/day")
def plan_day(
    plan_date: date = Form(...),
    choice: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Put one thing on one day: a recipe ("r:<id>") or a night off ("n:<title>")."""
    kind, _, value = choice.partition(":")
    if kind == "r" and value.isdigit():
        recipe = _owned(db, Recipe, int(value), user, "recipe")
        if _plan_recipe(db, user.id, recipe.id, plan_date, _default_servings(recipe)):
            db.flush()
            _sync_shopping(db, user.id)
    elif kind == "n" and value in NOTE_TITLES:
        taken = db.scalar(
            select(MealPlanEntry.id).where(
                MealPlanEntry.user_id == user.id,
                MealPlanEntry.plan_date == plan_date,
                MealPlanEntry.freeform_title == value,
            )
        )
        if taken is None:
            db.add(
                MealPlanEntry(
                    user_id=user.id,
                    plan_date=plan_date,
                    slot=MealSlot.DINNER,
                    freeform_title=value,
                )
            )
    return RedirectResponse(f"/plan#d-{plan_date.isoformat()}", status_code=303)


@app.post("/plan/{entry_id}/servings")
def set_plan_servings(
    entry_id: int,
    servings: int = Form(..., ge=1, le=MAX_SERVINGS),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
    if entry.recipe is None or entry.cooked_at is not None or entry.skipped_at is not None:
        raise HTTPException(status_code=404, detail="No such plan entry")
    entry.servings = servings
    entry.recipe.last_servings = servings
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/plan#d-{entry.plan_date.isoformat()}", status_code=303)


@app.post("/plan/{entry_id}/remove")
def remove_from_plan(
    entry_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    entry = db.get(MealPlanEntry, entry_id)
    if entry is not None and entry.user_id == user.id:
        db.delete(entry)
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse("/plan", status_code=303)


@app.post("/plan/{entry_id}/skip")
def skip_meal(
    entry_id: int,
    next_path: str = Form("/", alias="next"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Keep the plan entry and debit nothing. The meal didn't happen."""
    entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
    if entry.cooked_at is None and entry.skipped_at is None:
        entry.skipped_at = datetime.now(UTC)
        db.flush()
        _sync_shopping(db, user.id)
    return RedirectResponse(_safe_next(next_path), status_code=303)


@app.get("/audit", response_class=HTMLResponse)
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


@app.post("/audit")
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


@app.post("/audit/{ingredient_id}")
def resolve_audit_item(
    ingredient_id: int,
    quantity: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    _confirm_audit_field(db, user, ingredient_id, quantity)
    return RedirectResponse("/audit", status_code=303)


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


@app.get("/shopping", response_class=HTMLResponse)
def shopping_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render_shopping(request, db, user)


@app.post("/shopping/generate")
def generate_shopping(
    user: User = Depends(auth.current_user), db: Session = Depends(get_db)
) -> Response:
    _sync_shopping(db, user.id)
    return RedirectResponse("/shopping", status_code=303)


@app.post("/shopping/item/{item_id}/toggle")
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


@app.post("/shopping/stock")
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


@app.post("/shopping/add")
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
                request, db, user,
                add_error="Amount isn't a number.",
                add_name=name, add_amount=amount, add_unit=unit,
                status_code=400,
            )
    listing = live_list(db, user.id) or _sync_shopping(db, user.id)
    try:
        add_manual_item(db, listing, name, amount=parsed, unit=unit.strip() or None)
    except ShoppingError as exc:
        return _render_shopping(
            request, db, user,
            add_error=str(exc),
            add_name=name, add_amount=amount, add_unit=unit,
            status_code=400, rebuild=False,
        )
    return RedirectResponse("/shopping", status_code=303)


@app.post("/shopping/item/{item_id}/skip")
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


@app.get("/receipts", response_class=HTMLResponse)
def receipts_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    receipts = db.scalars(
        select(Receipt)
        .where(Receipt.user_id == user.id)
        .order_by(Receipt.created_at.desc())
        .limit(20)
    ).all()
    return _render(request, "receipts.html", receipts=receipts)


def _untick_stocked_lines(db: Session, user: User, receipt: Receipt) -> dict[int, str]:
    """Drop ticks on lines whose ingredient was bought from the list since it was built.

    The list's created_at is preserved across rebuilds, so a shop added on
    Monday still unticks Tuesday's receipt for those ingredients.
    """
    if receipt.status == ReceiptStatus.APPLIED:
        return {}
    listing = live_list(db, user.id)
    if listing is None or listing.created_at is None:
        return {}
    stocked = ingredients_stocked_since(db, user.id, listing.created_at)
    notes: dict[int, str] = {}
    for line in receipt.lines:
        if line.is_ignored or line.ingredient_id is None:
            continue
        if line.ingredient_id not in stocked:
            continue
        line.is_confirmed = False
        notes[line.id] = LIST_STOCK_NOTE
    if notes:
        db.flush()
    return notes


def _review_receipt(
    request: Request,
    db: Session,
    receipt: Receipt,
    *,
    error: str = "",
    status_code: int = 200,
    user: User | None = None,
) -> HTMLResponse:
    owner = user or getattr(request.state, "user", None)
    list_notes: dict[int, str] = {}
    if owner is not None and receipt.status != ReceiptStatus.APPLIED:
        list_notes = _untick_stocked_lines(db, owner, receipt)
    ingredients = db.scalars(
        select(CanonicalIngredient).order_by(CanonicalIngredient.name)
    ).all()
    visible = [line for line in receipt.lines if not line.is_ignored]
    ignored = [line for line in receipt.lines if line.is_ignored]
    return _render(
        request,
        "receipt_review.html",
        status_code=status_code,
        receipt=receipt,
        lines=visible,
        ignored=ignored,
        ingredients=ingredients,
        error=error,
        list_notes=list_notes,
        already=request.query_params.get("already") == "1",
    )


def _save_receipt_lines(db: Session, receipt: Receipt, form) -> None:  # noqa: ANN001
    """Write the review form onto the lines before applying them.

    Apply used to submit a form that contained none of the line fields, so
    unticked-then-ticked rows were discarded and the receipt locked anyway.
    """
    if receipt.status == ReceiptStatus.APPLIED:
        raise ReceiptError("This receipt has already been applied.")

    planned: list[tuple[ReceiptLine, int | None, float | None, str, bool]] = []
    for line in receipt.lines:
        if line.is_ignored:
            continue
        ingredient_key = f"ingredient_{line.id}"
        quantity_key = f"quantity_{line.id}"
        unit_key = f"unit_{line.id}"
        if ingredient_key not in form and quantity_key not in form and unit_key not in form:
            continue
        ingredient_raw = str(form.get(ingredient_key) or "").strip()
        quantity_raw = str(form.get(quantity_key) or "").strip()
        unit_raw = str(form.get(unit_key) or "").strip()
        ingredient_id = None
        if ingredient_raw:
            try:
                ingredient_id = int(ingredient_raw)
            except ValueError as exc:
                raise ReceiptError(f"'{line.raw_text}' has no ingredient.") from exc
        quantity = None
        if quantity_raw:
            try:
                quantity = float(quantity_raw)
            except ValueError as exc:
                raise ReceiptError(f"Amount for '{line.raw_text}' isn't a number.") from exc
            if quantity <= 0:
                raise ReceiptError(f"Amount for '{line.raw_text}' has to be more than zero.")
        confirmed = form.get(f"confirmed_{line.id}") in ("on", "true", "1")
        if confirmed and ingredient_id is None:
            confirmed = False
        planned.append((line, ingredient_id, quantity, unit_raw, confirmed))

    matcher = IngredientMatcher(db)
    for line, ingredient_id, quantity, unit_raw, confirmed in planned:
        if ingredient_id is not None and ingredient_id != line.ingredient_id:
            matcher.learn(
                line.raw_text, ingredient_id, source=AliasSource.MANUAL, is_receipt=True
            )
            line.match_confidence = 1.0
        line.ingredient_id = ingredient_id
        if quantity is not None:
            line.parsed_quantity = quantity
        if unit_raw:
            line.parsed_unit = unit_raw
        line.is_confirmed = confirmed
    db.flush()


@app.post("/receipts/upload")
async def upload_receipt(
    request: Request,
    image: UploadFile,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    data = await image.read()
    try:
        receipt = create_receipt(db, user.id, data, image.filename or "receipt.png")
        # Re-running OCR on an applied receipt wiped the confirmations and
        # reopened it. A photo that still needs review can be retried.
        if receipt.status == ReceiptStatus.APPLIED:
            return RedirectResponse(f"/receipts/{receipt.id}?already=1", status_code=303)
        scan_receipt(db, receipt)
    except ReceiptError as exc:
        receipts = db.scalars(
            select(Receipt)
            .where(Receipt.user_id == user.id)
            .order_by(Receipt.created_at.desc())
            .limit(20)
        ).all()
        return _render(
            request, "receipts.html", status_code=400, receipts=receipts, error=str(exc)
        )
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


@app.get("/receipts/{receipt_id}/image")
def receipt_image(
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """The photo, and only to the account that uploaded it."""
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    try:
        data = get_storage().read(image_key(receipt.image_path))
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="No such receipt") from exc
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/receipts/{receipt_id}", response_class=HTMLResponse)
def receipt_review(
    request: Request,
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    return _review_receipt(request, db, receipt)


@app.post("/receipts/{receipt_id}/line/{line_id}")
def update_receipt_line(
    request: Request,
    receipt_id: int,
    line_id: int,
    ingredient_id: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    confirmed: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # Ownership is checked on the receipt, then the line is checked to belong to
    # that receipt -- verifying only one of the two would let a line from
    # someone else's receipt be edited through a receipt id you do own.
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    if receipt.status == ReceiptStatus.APPLIED:
        return _review_receipt(
            request, db, receipt,
            error="This receipt has already been applied.", status_code=400,
        )
    line = db.get(ReceiptLine, line_id)
    if line is None or line.receipt_id != receipt_id:
        raise HTTPException(status_code=404, detail="No such line")

    if ingredient_id.strip():
        new_id = int(ingredient_id)
        # A manual correction is the strongest signal there is, so teach it.
        if new_id != line.ingredient_id:
            IngredientMatcher(db).learn(
                line.raw_text, new_id, source=AliasSource.MANUAL, is_receipt=True
            )
        line.ingredient_id = new_id
        line.match_confidence = 1.0

    if quantity.strip():
        try:
            parsed = float(quantity)
        except ValueError:
            return _review_receipt(
                request, db, receipt, error="Amount isn't a number.", status_code=400
            )
        if parsed <= 0:
            return _review_receipt(
                request, db, receipt,
                error="Amount has to be more than zero.", status_code=400,
            )
        line.parsed_quantity = parsed
    if unit.strip():
        line.parsed_unit = unit
    line.is_confirmed = confirmed == "on" and line.ingredient_id is not None
    db.flush()
    return RedirectResponse(f"/receipts/{receipt_id}", status_code=303)


def _added_label(line: ReceiptLine, ingredient: CanonicalIngredient) -> str:
    if not line.parsed_quantity:
        return ingredient.name
    amount = line.parsed_quantity
    text = str(int(round(amount))) if abs(amount - round(amount)) < 0.05 else f"{amount:g}"
    unit = (line.parsed_unit or "").strip()
    garlic = ingredient.name.casefold() == "garlic"
    if garlic and unit.casefold() in {"", "count", "clove", "cloves"}:
        word = "clove" if text == "1" else "cloves"
        return f"{ingredient.name} ({text} {word})"
    if unit and unit.casefold() != "count":
        return f"{ingredient.name} ({text} {unit})"
    if unit.casefold() == "count":
        return f"{ingredient.name} ({text} count)"
    return f"{ingredient.name} ({text})"


def _added_labels(db: Session, receipt: Receipt, warnings: list[str]) -> list[str]:
    failed = set()
    for warning in warnings:
        raw, sep, _rest = warning.partition(":")
        if sep:
            failed.add(raw)
    labels = []
    for line in receipt.lines:
        if line.is_ignored or not line.is_confirmed or line.ingredient_id is None:
            continue
        if line.raw_text in failed:
            continue
        ingredient = db.get(CanonicalIngredient, line.ingredient_id)
        if ingredient is None:
            continue
        labels.append(_added_label(line, ingredient))
    return labels


@app.get("/receipts/{receipt_id}/apply")
def apply_receipt_get(
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """A refresh after applying is a GET. The form lives on the review page."""
    _owned(db, Receipt, receipt_id, user, "receipt")
    return RedirectResponse(f"/receipts/{receipt_id}", status_code=303)


@app.post("/receipts/{receipt_id}/apply")
async def apply_receipt_route(
    request: Request,
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    form = await request.form()
    try:
        _save_receipt_lines(db, receipt, form)
        added, warnings = apply_receipt(db, receipt)
    except ReceiptError as exc:
        return _review_receipt(request, db, receipt, error=str(exc), status_code=400)

    if added == 0:
        return _review_receipt(
            request, db, receipt,
            error=warnings[-1] if warnings else "Tick a line before applying.",
            status_code=400,
        )
    return _render(
        request,
        "receipt_applied.html",
        receipt=receipt,
        warnings=warnings,
        added=added,
        added_labels=_added_labels(db, receipt, warnings),
    )


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
        invites = db.scalars(
            select(Invite).order_by(Invite.created_at.desc()).limit(20)
        ).all()

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


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _settings_page(request, db, user, saved=saved if saved == "password" else "")


@app.post("/settings/theme")
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


@app.post("/settings/staples")
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
            request, db, user, error="Enter the ingredient to assume.", status_code=400,
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
            name=clean, category=IngredientCategory.OTHER, base_unit="count",
        )
        db.add(ingredient)
        db.flush()
    ingredient.is_staple = True
    db.flush()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/staples/{ingredient_id}/clear")
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


@app.post("/settings/password")
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


@app.post("/settings/invites")
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


@app.post("/settings/invites/{invite_id}/revoke")
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


@app.get("/settings/{rest:path}")
def settings_form_get(
    rest: str,  # noqa: ARG001
    _user: User = Depends(auth.current_user),
) -> RedirectResponse:
    """A refresh of a settings form (password, staple, invite) lands back here."""
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/nyt-cookie")
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
        credential = StoredCredential(
            user_id=user.id, kind=CredentialKind.NYT_COOKING_COOKIE
        )
        db.add(credential)

    credential.ciphertext = encrypt(value)
    credential.label = f"Saved {date.today().isoformat()}"
    credential.is_working = True
    db.flush()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/nyt-cookie/delete")
def delete_nyt_cookie(
    user: User = Depends(auth.current_user), db: Session = Depends(get_db)
) -> Response:
    credential = _nyt_credential(db, user)
    if credential is not None:
        db.delete(credential)
        db.flush()
    return RedirectResponse("/settings", status_code=303)


@app.get("/share")
def share_target(
    url: str = "",
    text: str = "",
    title: str = "",
    _user: User = Depends(auth.current_user),
) -> Response:
    """Android share-sheet entry point for a recipe URL.

    Shared text often contains a URL plus commentary, so the URL is pulled out
    of whichever field carried it.
    """
    import re

    candidate = url or text or title
    found = re.search(r"https?://\S+", candidate or "")
    if not found:
        return RedirectResponse("/recipes", status_code=303)
    return RedirectResponse(f"/recipes?import_url={found.group()}", status_code=303)


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "SousChef",
            "short_name": "SousChef",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#F8F6F1",
            "theme_color": "#F8F6F1",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
            "share_target": {
                "action": "/share",
                "method": "GET",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        },
        media_type="application/manifest+json",
    )


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    """Liveness plus the two misconfigurations that break the app silently.

    An app that answers 200 while unable to decrypt credentials or issue
    sessions is worse than one that is plainly down, because nothing surfaces
    the problem until you try to sign in.
    """
    problems: list[str] = []
    try:
        db.execute(select(func.count()).select_from(CanonicalIngredient))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log.exception("Health check: database unreachable")
        problems.append(f"database: {exc}")

    settings = get_settings()
    if not settings.session_secret:
        problems.append("SOUSCHEF_SESSION_SECRET is not set")
    if not settings.secret_key:
        problems.append("SOUSCHEF_SECRET_KEY is not set")

    if problems:
        log.error("Health check failing: %s", "; ".join(problems))
        return JSONResponse({"status": "degraded", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ok"})
