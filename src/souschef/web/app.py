"""FastAPI application: routes, templates, and the PWA shell."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from souschef import logconfig
from souschef.config import ensure_local_secrets, get_settings
from souschef.core.audit import build_audit
from souschef.core.crypto import encrypt
from souschef.core.inventory import (
    confirm_level,
    consume_for_recipe,
    pantry_view,
    record_event,
    set_level,
)
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.recipes import recipe_requirements, save_imported_recipe
from souschef.core.units import UnitConversionError, dimension_of, humanize
from souschef.db import get_db
from souschef.ingest.receipt_service import (
    ReceiptError,
    apply_receipt,
    create_receipt,
    scan_receipt,
)
from souschef.ingest.recipes.importers import ImportError_, import_from_url
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    Invite,
    MealPlanEntry,
    MealSlot,
    Receipt,
    ReceiptLine,
    Recipe,
    ShoppingListItem,
    StoredCredential,
    User,
)
from souschef.models.base import Base
from souschef.models.ingredient import AliasSource, IngredientCategory
from souschef.models.settings import CredentialKind
from souschef.planning.shopping import build_shopping_list, requirements_for_audit
from souschef.planning.suggest import Surface, by_surface, suggest
from souschef.web import auth

PLAN_DAYS = 10

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["humanize"] = humanize

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


def _render(request: Request, template: str, **context) -> HTMLResponse:
    # `user` is injected rather than passed by each route: the nav and sign-out
    # control need it on every authenticated page, and a route that forgot it
    # would silently render the signed-out chrome to a signed-in user.
    context.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, template, context)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


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


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    # An empty database means nobody has claimed this instance yet. Whoever
    # reaches it first becomes the admin.
    if auth.user_count(db) == 0:
        return _render(request, "setup.html")
    return _render(request, "login.html")


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
    invite = auth.find_usable_invite(db, code) if code else None
    return _render(
        request,
        "join.html",
        code=code,
        invite_ok=invite is not None,
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
    db: Session = Depends(get_db),
) -> Response:
    ip = auth.client_key(request)
    if auth.is_locked_out(ip, username):
        return _render(
            request, "login.html", username=username,
            error="Too many attempts. Try again in a few minutes.",
        )

    user = auth.authenticate(db, username, password)
    if user is None:
        auth.record_failure(ip, username)
        # One message for both "no such user" and "wrong password": telling
        # them apart is a free list of who has an account here.
        return _render(
            request, "login.html", username=username,
            error="Incorrect username or password.",
        )

    auth.clear_failures(ip, username)
    response = RedirectResponse("/", status_code=303)
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
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    suggestions = suggest(db, user.id, limit=12)
    grouped = by_surface(suggestions)

    today = date.today()
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
    expiring = sorted(
        (v for v in pantry if v.item.expires_on and (v.item.expires_on - today).days <= 5),
        key=lambda v: v.item.expires_on,
    )
    stale = sorted((v for v in pantry if v.confidence < 0.5), key=lambda v: v.confidence)[:5]

    return _render(
        request,
        "dashboard.html",
        cook_now=grouped[Surface.COOK_NOW][:10],
        nearly=grouped[Surface.NEARLY_THERE][:10],
        bigger_shop=grouped[Surface.REACH][:10],
        upcoming=upcoming,
        expiring=expiring[:5],
        stale=stale,
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
        score = 100.0 if needle in name else fuzz.partial_ratio(needle, name)
        if score >= PANTRY_SEARCH_CUTOFF:
            scored.append((score, view))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [view for _, view in scored]


@app.get("/pantry", response_class=HTMLResponse)
def pantry_page(
    request: Request,
    q: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    views = pantry_view(db, user.id, include_empty=True)
    if q.strip():
        views = _search_pantry(views, q)

    by_location: dict[str, list] = {}
    for view in views:
        by_location.setdefault(str(view.item.location), []).append(view)

    # A live-search keystroke swaps in only the list; a full navigation renders
    # the page around it.
    template = "pantry_list.html" if _is_htmx(request) else "pantry.html"
    return _render(request, template, by_location=by_location, query=q)


@app.post("/pantry/{ingredient_id}/level")
def update_level(
    request: Request,
    ingredient_id: int,
    quantity: float = Form(...),
    unit: str = Form(""),
    q: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # The ingredient catalog is shared, so no ownership check applies to it --
    # the scoping happens on the pantry row `set_level` writes.
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    set_level(db, user.id, ingredient, quantity, unit=unit or None)
    return RedirectResponse(_pantry_url(q), status_code=303)


@app.post("/pantry/{ingredient_id}/confirm")
def confirm_item(
    ingredient_id: int,
    q: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    confirm_level(db, user.id, ingredient)
    return RedirectResponse(_pantry_url(q), status_code=303)


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
    except UnitConversionError:
        return "count", None
    return {"mass": "g", "volume": "ml"}.get(dim, "count"), cleaned


@app.post("/pantry/add")
def add_to_pantry(
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
    if not clean_name:
        raise HTTPException(status_code=400, detail="Ingredient name is empty")

    match = IngredientMatcher(db).match(clean_name)
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
    return RedirectResponse(_pantry_url(q), status_code=303)


@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    suggestions = suggest(db, user.id, limit=200)
    return _render(request, "recipes.html", grouped=by_surface(suggestions))


@app.get("/recipes/{recipe_id}", response_class=HTMLResponse)
def recipe_detail(
    request: Request,
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    requirements = recipe_requirements(db, recipe)
    pantry = {v.ingredient.id: v for v in pantry_view(db, user.id)}
    return _render(
        request, "recipe_detail.html", recipe=recipe, requirements=requirements, pantry=pantry
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
        return _render(request, "import_result.html", error=str(exc))

    recipe = save_imported_recipe(
        db, user.id, result.recipe, source=result.source, external_id=result.external_id
    )
    return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)


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
        servings = servings or plan_entry.servings
    servings = servings or recipe.servings or 1
    requirements = recipe_requirements(db, recipe, servings=servings)
    pantry = {v.ingredient.id: v for v in pantry_view(db, user.id)}

    debits: list[dict] = []
    by_id: dict[int, dict] = {}
    for req in requirements.requirements:
        if req.quantity <= 0:
            continue
        row = by_id.get(req.ingredient.id)
        if row is None:
            have = pantry[req.ingredient.id].quantity if req.ingredient.id in pantry else 0.0
            row = {
                "ingredient": req.ingredient,
                "quantity": 0.0,
                "is_staple": req.is_staple,
                "have": have,
            }
            by_id[req.ingredient.id] = row
            debits.append(row)
        row["quantity"] += req.quantity
        row["is_staple"] = row["is_staple"] or req.is_staple

    return _render(
        request,
        "cook_preview.html",
        recipe=recipe,
        debits=debits,
        servings=servings,
        entry=plan_entry,
    )


@app.post("/recipes/{recipe_id}/cooked")
async def mark_cooked(
    request: Request,
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    form = await request.form()
    servings = int(form.get("servings") or 0)
    entry_id = int(form.get("entry_id") or 0)

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
    consume_for_recipe(db, user.id, used, recipe_id=recipe.id, notes=notes or None)

    recipe.times_cooked += 1
    recipe.last_cooked_at = datetime.now(UTC)
    if entry_id:
        plan_entry = _owned(db, MealPlanEntry, entry_id, user, "plan entry")
        if plan_entry.recipe_id == recipe.id:
            plan_entry.cooked_at = datetime.now(UTC)
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)


@app.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    today, week_end = _horizon()
    entries = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.plan_date >= today,
            MealPlanEntry.plan_date <= week_end,
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()

    days = [today + timedelta(days=offset) for offset in range(PLAN_DAYS)]
    by_day = {day: [e for e in entries if e.plan_date == day] for day in days}

    return _render(
        request,
        "plan.html",
        by_day=by_day,
        days=days,
        has_meals=any(by_day.values()),
        suggestions=suggest(db, user.id, limit=10),
        week_end=week_end,
    )


@app.post("/plan/add")
async def add_to_plan(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    recipe_id = int(form.get("recipe_id") or 0)
    servings = int(form.get("servings") or 4)
    # Validate the recipe is theirs before planning it: without this, posting an
    # arbitrary id would put another user's recipe on your calendar, and the
    # plan page would then render its title.
    _owned(db, Recipe, recipe_id, user, "recipe")
    for raw in form.getlist("plan_date"):
        value = str(raw).strip()
        if not value:
            continue
        db.add(
            MealPlanEntry(
                user_id=user.id,
                plan_date=date.fromisoformat(value),
                slot=MealSlot.DINNER,
                recipe_id=recipe_id,
                servings=servings,
            )
        )
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse("/plan", status_code=303)


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


@app.post("/audit/{ingredient_id}")
def resolve_audit_item(
    ingredient_id: int,
    quantity: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")

    # An empty box means "the estimate was right", which still restores
    # confidence -- that is the whole point of asking.
    confirm_level(db, user.id, ingredient, float(quantity) if quantity.strip() else None)
    return RedirectResponse("/audit", status_code=303)


@app.get("/shopping", response_class=HTMLResponse)
def shopping_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    start, end = _horizon()
    active = _sync_shopping(db, user.id)
    has_plan = (
        db.scalar(
            select(func.count())
            .select_from(MealPlanEntry)
            .where(
                MealPlanEntry.user_id == user.id,
                MealPlanEntry.plan_date >= start,
                MealPlanEntry.plan_date <= end,
                MealPlanEntry.cooked_at.is_(None),
            )
        )
        or 0
    ) > 0

    by_aisle: dict[str, list] = {}
    for item in sorted(active.items, key=lambda i: (i.aisle or "", i.sort_order)):
        by_aisle.setdefault(item.aisle or "Other", []).append(item)

    return _render(
        request,
        "shopping.html",
        shopping_list=active,
        by_aisle=by_aisle,
        has_plan=has_plan,
    )


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


@app.post("/receipts/upload")
async def upload_receipt(
    image: UploadFile,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    data = await image.read()
    try:
        receipt = create_receipt(db, user.id, data, image.filename or "receipt.png")
        scan_receipt(db, receipt)
    except ReceiptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


@app.get("/receipts/{receipt_id}", response_class=HTMLResponse)
def receipt_review(
    request: Request,
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")

    ingredients = db.scalars(
        select(CanonicalIngredient).order_by(CanonicalIngredient.name)
    ).all()
    items = [line for line in receipt.lines if not line.is_ignored]
    return _render(
        request, "receipt_review.html", receipt=receipt, lines=items, ingredients=ingredients
    )


@app.post("/receipts/{receipt_id}/line/{line_id}")
def update_receipt_line(
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
    _owned(db, Receipt, receipt_id, user, "receipt")
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
        line.parsed_quantity = float(quantity)
    if unit.strip():
        line.parsed_unit = unit
    line.is_confirmed = confirmed == "on"
    db.flush()
    return RedirectResponse(f"/receipts/{receipt_id}", status_code=303)


@app.post("/receipts/{receipt_id}/apply")
def apply_receipt_route(
    request: Request,
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    try:
        warnings = apply_receipt(db, receipt)
    except ReceiptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if warnings:
        return _render(request, "receipt_applied.html", receipt=receipt, warnings=warnings)
    return RedirectResponse("/pantry", status_code=303)


def _nyt_credential(db: Session, user: User) -> StoredCredential | None:
    return db.scalar(
        select(StoredCredential).where(
            StoredCredential.user_id == user.id,
            StoredCredential.kind == CredentialKind.NYT_COOKING_COOKIE,
        )
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
    new_invite: str = "",
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

    return _render(
        request,
        "settings.html",
        nyt_credential=_nyt_credential(db, user),
        settings=get_settings(),
        invites=invites,
        staples=staples,
        # Shown once, immediately after creation. The plaintext is never stored,
        # so there is no page that can display it again.
        new_invite=new_invite,
        now=datetime.now(UTC),
    )


@app.post("/settings/staples")
def add_staple(
    name: str = Form(...),
    _user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    match = IngredientMatcher(db).match(name)
    if not match.is_match:
        raise HTTPException(status_code=400, detail=f"Unknown ingredient: {name}")
    ingredient = db.get(CanonicalIngredient, match.ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
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
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if password != confirm:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    try:
        auth.set_password(db, user, password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # set_password bumped the session epoch, which just invalidated this
    # request's own cookie along with every other one. Re-issue so the person
    # who changed their password isn't the only one signed out by it.
    response = RedirectResponse("/settings", status_code=303)
    _set_session_cookie(request, response, user)
    return response


@app.post("/settings/invites")
def create_invite_route(
    note: str = Form(""),
    user: User = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> Response:
    _, code = auth.create_invite(db, user, note=note)
    # The code rides back in the URL so the page can show it once. That puts it
    # in this user's own browser history, which is an acceptable trade for not
    # storing the plaintext anywhere on the server.
    return RedirectResponse(f"/settings?new_invite={code}", status_code=303)


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


@app.post("/settings/nyt-cookie")
def save_nyt_cookie(
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
        raise HTTPException(status_code=400, detail="Cookie is empty")

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
            "background_color": "#12151a",
            "theme_color": "#12151a",
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
