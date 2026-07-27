"""FastAPI application: routes, templates, and the PWA shell."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from souschef.config import get_settings
from souschef.core.audit import build_audit
from souschef.core.crypto import encrypt
from souschef.core.inventory import (
    confirm_level,
    consume_for_recipe,
    pantry_view,
    record_event,
    set_level,
)
from souschef.core.matching import IngredientMatcher
from souschef.core.recipes import recipe_requirements, save_imported_recipe
from souschef.core.units import humanize
from souschef.db import get_db
from souschef.ingest.receipt_service import (
    ReceiptError,
    apply_receipt,
    create_receipt,
    scan_receipt,
)
from souschef.ingest.recipes.importers import ImportError_, import_from_url
from souschef.models import (
    AppSetting,
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    Receipt,
    ReceiptLine,
    Recipe,
    ShoppingList,
    ShoppingListStatus,
    StoredCredential,
)
from souschef.models.ingredient import AliasSource
from souschef.models.settings import CredentialKind
from souschef.planning.shopping import build_shopping_list, requirements_for_audit
from souschef.planning.suggest import Surface, by_surface, suggest
from souschef.web import auth

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["humanize"] = humanize

app = FastAPI(title="SousChef", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR.parent / "static")), name="static")

PASSWORD_HASH_KEY = "auth.password_hash"


# --------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------


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
    return templates.TemplateResponse(request, template, context)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------


def _stored_password_hash(db: Session) -> str | None:
    setting = db.get(AppSetting, PASSWORD_HASH_KEY)
    return setting.value if setting else None


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if _stored_password_hash(db) is None:
        return _render(request, "setup.html")
    return _render(request, "login.html")


@app.post("/setup")
def setup(
    request: Request,
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    if _stored_password_hash(db) is not None:
        raise HTTPException(status_code=400, detail="Already set up")
    if password != confirm:
        return _render(request, "setup.html", error="Passwords do not match.")
    try:
        hashed = auth.hash_password(password)
    except ValueError as exc:
        return _render(request, "setup.html", error=str(exc))

    db.add(AppSetting(key=PASSWORD_HASH_KEY, value=hashed))
    db.flush()

    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response)
    return response


def _is_secure_request(request: Request) -> bool:
    """Whether the *browser* reached us over HTTPS.

    Caddy terminates TLS, so the app itself always sees http and has to trust
    the forwarded header. Keying the Secure flag off the scheme rather than the
    hostname matters: a Secure cookie is never sent back over plain HTTP, so
    deriving it from the hostname silently breaks every non-localhost HTTP
    deployment -- sign-in appears to succeed and the next request is anonymous.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


def _set_session_cookie(request: Request, response: Response) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_secure_request(request),
    )


@app.post("/login")
def login(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    key = auth.client_key(request)
    if auth.is_locked_out(key):
        return _render(
            request, "login.html", error="Too many attempts. Try again in a few minutes."
        )

    stored = _stored_password_hash(db)
    if stored is None or not auth.verify_password(stored, password):
        auth.record_failure(key)
        return _render(request, "login.html", error="Incorrect password.")

    auth.clear_failures(key)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(request, response)
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    suggestions = suggest(db, limit=12)
    grouped = by_surface(suggestions)

    today = date.today()
    upcoming = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(MealPlanEntry.plan_date >= today, MealPlanEntry.cooked_at.is_(None))
        .order_by(MealPlanEntry.plan_date)
        .limit(7)
    ).all()

    pantry = pantry_view(db)
    expiring = sorted(
        (v for v in pantry if v.item.expires_on and (v.item.expires_on - today).days <= 5),
        key=lambda v: v.item.expires_on,
    )
    stale = sorted(pantry, key=lambda v: v.confidence)[:5]

    return _render(
        request,
        "dashboard.html",
        cook_now=grouped[Surface.COOK_NOW][:4],
        nearly=grouped[Surface.NEARLY_THERE][:4],
        upcoming=upcoming,
        expiring=expiring[:5],
        stale=stale,
        pantry_count=len(pantry),
        recipe_count=db.scalar(select(func.count()).select_from(Recipe)) or 0,
    )


# --------------------------------------------------------------------------
# Pantry
# --------------------------------------------------------------------------


@app.get("/pantry", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def pantry_page(
    request: Request, q: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    views = pantry_view(db, include_empty=True)
    if q:
        needle = q.lower()
        views = [v for v in views if needle in v.ingredient.name.lower()]

    by_location: dict[str, list] = {}
    for view in views:
        by_location.setdefault(str(view.item.location), []).append(view)

    return _render(request, "pantry.html", by_location=by_location, query=q)


@app.post("/pantry/{ingredient_id}/level", dependencies=[Depends(auth.require_session)])
def update_level(
    request: Request,
    ingredient_id: int,
    quantity: float = Form(...),
    unit: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    set_level(db, ingredient, quantity, unit=unit or None)
    return RedirectResponse("/pantry", status_code=303)


@app.post("/pantry/{ingredient_id}/confirm", dependencies=[Depends(auth.require_session)])
def confirm_item(
    ingredient_id: int, db: Session = Depends(get_db)
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")
    confirm_level(db, ingredient)
    return RedirectResponse("/pantry", status_code=303)


@app.post("/pantry/add", dependencies=[Depends(auth.require_session)])
def add_to_pantry(
    name: str = Form(...),
    quantity: float = Form(...),
    unit: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    match = IngredientMatcher(db).match(name)
    if not match.is_match:
        raise HTTPException(status_code=400, detail=f"Unknown ingredient: {name}")
    ingredient = db.get(CanonicalIngredient, match.ingredient_id)
    record_event(
        db, ingredient, reason=EventReason.PURCHASE, delta=quantity, unit=unit or None
    )
    return RedirectResponse("/pantry", status_code=303)


# --------------------------------------------------------------------------
# Recipes
# --------------------------------------------------------------------------


@app.get("/recipes", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def recipes_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    suggestions = suggest(db, limit=200)
    return _render(request, "recipes.html", grouped=by_surface(suggestions))


@app.get(
    "/recipes/{recipe_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(auth.require_session)],
)
def recipe_detail(
    request: Request, recipe_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="No such recipe")
    requirements = recipe_requirements(db, recipe)
    pantry = {v.ingredient.id: v for v in pantry_view(db)}
    return _render(
        request, "recipe_detail.html", recipe=recipe, requirements=requirements, pantry=pantry
    )


@app.post("/recipes/import", dependencies=[Depends(auth.require_session)])
def import_recipe(
    request: Request, url: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    try:
        result = import_from_url(db, url)
    except ImportError_ as exc:
        return _render(request, "import_result.html", error=str(exc))

    recipe = save_imported_recipe(
        db, result.recipe, source=result.source, external_id=result.external_id
    )
    return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)


@app.post("/recipes/{recipe_id}/cooked", dependencies=[Depends(auth.require_session)])
def mark_cooked(
    recipe_id: int, servings: int = Form(0), db: Session = Depends(get_db)
) -> Response:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="No such recipe")

    requirements = recipe_requirements(db, recipe, servings=servings or None)
    consume_for_recipe(db, requirements.as_map(), recipe_id=recipe.id)

    recipe.times_cooked += 1
    recipe.last_cooked_at = datetime.now(UTC)
    db.flush()
    return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)


# --------------------------------------------------------------------------
# Planning, audit and shopping
# --------------------------------------------------------------------------


@app.get("/plan", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def plan_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today = date.today()
    week_end = today + timedelta(days=7)
    entries = db.scalars(
        select(MealPlanEntry)
        .options(selectinload(MealPlanEntry.recipe))
        .where(MealPlanEntry.plan_date >= today, MealPlanEntry.plan_date <= week_end)
        .order_by(MealPlanEntry.plan_date)
    ).all()

    days = [today + timedelta(days=offset) for offset in range(8)]
    by_day = {day: [e for e in entries if e.plan_date == day] for day in days}

    return _render(
        request,
        "plan.html",
        by_day=by_day,
        suggestions=suggest(db, limit=10),
        week_end=week_end,
    )


@app.post("/plan/add", dependencies=[Depends(auth.require_session)])
def add_to_plan(
    recipe_id: int = Form(...),
    plan_date: str = Form(...),
    servings: int = Form(4),
    db: Session = Depends(get_db),
) -> Response:
    db.add(
        MealPlanEntry(
            plan_date=date.fromisoformat(plan_date),
            slot=MealSlot.DINNER,
            recipe_id=recipe_id,
            servings=servings,
        )
    )
    db.flush()
    return RedirectResponse("/plan", status_code=303)


@app.post("/plan/{entry_id}/remove", dependencies=[Depends(auth.require_session)])
def remove_from_plan(entry_id: int, db: Session = Depends(get_db)) -> Response:
    entry = db.get(MealPlanEntry, entry_id)
    if entry is not None:
        db.delete(entry)
        db.flush()
    return RedirectResponse("/plan", status_code=303)


@app.get("/audit", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def audit_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The pre-shop check: the few things worth physically verifying."""
    today = date.today()
    week_end = today + timedelta(days=7)
    requirements = requirements_for_audit(db, today, week_end)
    candidates = build_audit(db, requirements)
    return _render(
        request,
        "audit.html",
        candidates=candidates,
        checked_everything=not candidates,
        planned_items=len(requirements),
    )


@app.post("/audit/{ingredient_id}", dependencies=[Depends(auth.require_session)])
def resolve_audit_item(
    ingredient_id: int,
    quantity: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    ingredient = db.get(CanonicalIngredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")

    # An empty box means "the estimate was right", which still restores
    # confidence -- that is the whole point of asking.
    confirm_level(db, ingredient, float(quantity) if quantity.strip() else None)
    return RedirectResponse("/audit", status_code=303)


@app.get("/shopping", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def shopping_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    active = db.scalars(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(ShoppingList.status != ShoppingListStatus.ARCHIVED)
        .order_by(ShoppingList.created_at.desc())
    ).first()

    by_aisle: dict[str, list] = {}
    if active:
        for item in sorted(active.items, key=lambda i: (i.aisle or "", i.sort_order)):
            by_aisle.setdefault(item.aisle or "Other", []).append(item)

    return _render(request, "shopping.html", shopping_list=active, by_aisle=by_aisle)


@app.post("/shopping/generate", dependencies=[Depends(auth.require_session)])
def generate_shopping(db: Session = Depends(get_db)) -> Response:
    today = date.today()
    build_shopping_list(db, covers_from=today, covers_to=today + timedelta(days=7))
    return RedirectResponse("/shopping", status_code=303)


@app.post("/shopping/item/{item_id}/toggle", dependencies=[Depends(auth.require_session)])
def toggle_shopping_item(item_id: int, db: Session = Depends(get_db)) -> Response:
    from souschef.models import ShoppingListItem

    item = db.get(ShoppingListItem, item_id)
    if item is not None:
        item.is_checked = not item.is_checked
        db.flush()
    return RedirectResponse("/shopping", status_code=303)


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


@app.get("/receipts", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def receipts_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    receipts = db.scalars(select(Receipt).order_by(Receipt.created_at.desc()).limit(20)).all()
    return _render(request, "receipts.html", receipts=receipts)


@app.post("/receipts/upload", dependencies=[Depends(auth.require_session)])
async def upload_receipt(image: UploadFile, db: Session = Depends(get_db)) -> Response:
    data = await image.read()
    try:
        receipt = create_receipt(db, data, image.filename or "receipt.png")
        scan_receipt(db, receipt)
    except ReceiptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


@app.get(
    "/receipts/{receipt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(auth.require_session)],
)
def receipt_review(
    request: Request, receipt_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="No such receipt")

    ingredients = db.scalars(
        select(CanonicalIngredient).order_by(CanonicalIngredient.name)
    ).all()
    items = [line for line in receipt.lines if not line.is_ignored]
    return _render(
        request, "receipt_review.html", receipt=receipt, lines=items, ingredients=ingredients
    )


@app.post("/receipts/{receipt_id}/line/{line_id}", dependencies=[Depends(auth.require_session)])
def update_receipt_line(
    receipt_id: int,
    line_id: int,
    ingredient_id: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    confirmed: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
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


@app.post("/receipts/{receipt_id}/apply", dependencies=[Depends(auth.require_session)])
def apply_receipt_route(
    request: Request, receipt_id: int, db: Session = Depends(get_db)
) -> Response:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="No such receipt")
    try:
        warnings = apply_receipt(db, receipt)
    except ReceiptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if warnings:
        return _render(request, "receipt_applied.html", receipt=receipt, warnings=warnings)
    return RedirectResponse("/pantry", status_code=303)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth.require_session)])
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    credential = db.scalar(
        select(StoredCredential).where(
            StoredCredential.kind == CredentialKind.NYT_COOKING_COOKIE
        )
    )
    return _render(
        request, "settings.html", nyt_credential=credential, settings=get_settings()
    )


@app.post("/settings/nyt-cookie", dependencies=[Depends(auth.require_session)])
def save_nyt_cookie(cookie: str = Form(...), db: Session = Depends(get_db)) -> Response:
    """Store the NYT session cookie, encrypted.

    Only the cookie is accepted. The password is never asked for, because NYT
    gates login behind bot detection and a stored password would be a much
    larger prize on an internet-facing box.
    """
    value = cookie.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Cookie is empty")

    credential = db.scalar(
        select(StoredCredential).where(
            StoredCredential.kind == CredentialKind.NYT_COOKING_COOKIE
        )
    )
    if credential is None:
        credential = StoredCredential(kind=CredentialKind.NYT_COOKING_COOKIE)
        db.add(credential)

    credential.ciphertext = encrypt(value)
    credential.label = f"Saved {date.today().isoformat()}"
    credential.is_working = True
    db.flush()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/nyt-cookie/delete", dependencies=[Depends(auth.require_session)])
def delete_nyt_cookie(db: Session = Depends(get_db)) -> Response:
    credential = db.scalar(
        select(StoredCredential).where(
            StoredCredential.kind == CredentialKind.NYT_COOKING_COOKIE
        )
    )
    if credential is not None:
        db.delete(credential)
        db.flush()
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------------------
# PWA plumbing
# --------------------------------------------------------------------------


@app.get("/share", dependencies=[Depends(auth.require_session)])
def share_target(
    url: str = "", text: str = "", title: str = ""
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
def healthz(db: Session = Depends(get_db)) -> dict:
    db.execute(select(func.count()).select_from(CanonicalIngredient))
    return {"status": "ok"}
