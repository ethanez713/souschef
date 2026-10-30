"""Routes: recipes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import pantry_view
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.recipes import (
    create_manual_recipe,
    edit_recipe,
    find_saved_recipe,
    recipe_requirements,
    save_imported_recipe,
)
from souschef.core.units import UnitConversionError, dimension_of
from souschef.db import get_db
from souschef.ingest.recipes.importers import ImportError_, import_from_url
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.ingest.recipes.paste import parse_pasted_recipe
from souschef.models import (
    CanonicalIngredient,
    Cook,
    MealPlanEntry,
    Recipe,
    RecipeIngredient,
    RecipeSource,
    User,
)
from souschef.models.ingredient import AliasSource, IngredientCategory
from souschef.planning.suggest import by_surface, probably_gone_ids, suggest
from souschef.web import auth
from souschef.web.common import (
    MAX_SERVINGS,
    PLAN_DAYS,
    _default_servings,
    _horizon,
    _is_htmx,
    _kitchen_unit_message,
    _owned,
    _render,
    _sync_shopping,
)

router = APIRouter()


class NameMatchError(ValueError):
    """The cook's name could not be applied to the recipe line."""


_MAX_TITLE = 300
_MAX_URL = 1000
_MAX_LINE = 500
_MAX_MINUTES = 10_000


def _base_unit_for_named_line(unit: str) -> str:
    """Base unit for an ingredient named from a recipe line.

    Recipe lines use count words ("clove", "bunch") that the add form treats as
    packages. A recognised dimension wins here, and an unknown word stays a count
    so naming the line still saves.
    """
    cleaned = unit.strip()
    if not cleaned:
        return "count"
    try:
        dim = dimension_of(cleaned)
    except UnitConversionError as exc:
        if "Don't know how much" in str(exc):
            raise
        return "count"
    return {"mass": "g", "volume": "ml"}.get(dim, "count")


def _field_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def _steps_text(instructions: str | None) -> str:
    if not instructions:
        return ""
    steps = []
    for part in instructions.split("\n\n"):
        cleaned = " ".join(part.split())
        if cleaned:
            steps.append(cleaned)
    return "\n".join(steps)


def _whole_number(value: str) -> int | None:
    text = value.strip()
    if not text.isascii() or not text.isdecimal():
        return None
    return int(text)


class _PostedRecipe:
    def __init__(
        self,
        title: str,
        servings: int,
        total_minutes: int | None,
        source_url: str | None,
        ingredient_lines: list[str],
        instructions: str | None,
    ) -> None:
        self.title = title
        self.servings = servings
        self.total_minutes = total_minutes
        self.source_url = source_url
        self.ingredient_lines = ingredient_lines
        self.instructions = instructions


def _parse_recipe_form(
    title: str,
    servings: str,
    minutes: str,
    source_url: str,
    ingredients: str,
    method: str,
) -> tuple[_PostedRecipe | None, str]:
    clean_title = title.strip()
    if not clean_title:
        return None, "Enter a title."
    if len(clean_title) > _MAX_TITLE:
        return None, "Title is too long."

    count = _whole_number(servings)
    if count is None or not 1 <= count <= MAX_SERVINGS:
        return None, f"Servings is a whole number from 1 to {MAX_SERVINGS}."

    total_minutes = None
    if minutes.strip():
        total_minutes = _whole_number(minutes)
        if total_minutes is None or not 1 <= total_minutes <= _MAX_MINUTES:
            return None, "Minutes is a whole number, or leave it blank."

    url = source_url.strip()
    if url:
        if len(url) > _MAX_URL:
            return None, "Source URL is too long."
        if any(ch.isspace() for ch in url) or not url.lower().startswith(("http://", "https://")):
            return None, "Source URL starts with http:// or https://."

    lines = _field_lines(ingredients)
    if not lines:
        return None, "Enter the ingredients, one per line."
    if any(len(line) > _MAX_LINE for line in lines):
        return None, "An ingredient line is too long."

    steps = _field_lines(method)
    return _PostedRecipe(
        clean_title,
        count,
        total_minutes,
        url or None,
        lines,
        "\n\n".join(steps) or None,
    ), ""


def _render_recipe_form(
    request: Request,
    *,
    heading: str,
    action: str,
    cancel: str,
    title: str = "",
    servings: str = "4",
    minutes: str = "",
    source_url: str = "",
    ingredients: str = "",
    method: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "recipe_form.html",
        status_code=status_code,
        heading=heading,
        action=action,
        cancel=cancel,
        title=title,
        servings=servings,
        minutes=minutes,
        source_url=source_url,
        ingredients=ingredients,
        method=method,
        error=error,
    )


def _form_fields(recipe: Recipe) -> dict[str, str]:
    return {
        "title": recipe.title,
        "servings": str(recipe.servings or 4),
        "minutes": "" if recipe.total_minutes is None else str(recipe.total_minutes),
        "source_url": recipe.source_url or "",
        "ingredients": "\n".join(line.raw_text for line in recipe.ingredients),
        "method": _steps_text(recipe.instructions),
    }


def _save_import(
    db: Session,
    user: User,
    raw: RawRecipe,
    source: RecipeSource,
    external_id: str | None,
) -> Recipe:
    """Persist an import and rebuild the shop list when it replaced a recipe's lines."""
    replacing = (
        find_saved_recipe(
            db,
            user.id,
            source=source,
            external_id=external_id,
            source_url=raw.source_url,
        )
        is not None
    )
    recipe = save_imported_recipe(db, user.id, raw, source=source, external_id=external_id)
    if replacing:
        _sync_shopping(db, user.id)
    return recipe


@router.get("/recipes", response_class=HTMLResponse)
def recipes_page(
    request: Request,
    q: str = "",
    import_url: str = "",
    archived: int = 0,
    toast: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    suggestions = suggest(db, user.id, limit=200)
    needle = q.strip().casefold()
    if needle:
        suggestions = [s for s in suggestions if _recipe_matches(s.recipe, needle)]
    archived_recipe = db.get(Recipe, archived) if archived else None
    if (
        archived_recipe is None
        or archived_recipe.user_id != user.id
        or not archived_recipe.is_archived
    ):
        archived_recipe = None
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
        archived=archived_recipe,
        toast_message="" if archived_recipe is not None or toast != "Restored" else "Restored",
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
        if line.is_ignored:
            rows.append({"req": None, "raw": line.raw_text, "line": line})
            continue
        bucket = pools.get(line.raw_text)
        if bucket:
            rows.append({"req": bucket.pop(0), "raw": line.raw_text, "line": line})
        else:
            rows.append({"req": None, "raw": line.raw_text, "line": line})
            unmatched += 1
    for bucket in pools.values():
        rows.extend({"req": req, "raw": req.raw_text, "line": None} for req in bucket)
    return rows, unmatched


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _latest_cook(db: Session, user_id: int, recipe_id: int) -> Cook | None:
    """The recipe's newest cook, undone or not. An undone latest row hides older ones."""
    return db.scalar(
        select(Cook)
        .where(Cook.user_id == user_id, Cook.recipe_id == recipe_id)
        .order_by(Cook.cooked_at.desc(), Cook.id.desc())
    )


def _planned_message(raw: str) -> str:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts or len(parts) > PLAN_DAYS:
        return ""
    days: list[date] = []
    for part in parts:
        if len(part) != 10:
            return ""
        try:
            days.append(date.fromisoformat(part))
        except ValueError:
            return ""
    labels = [day.strftime("%a %-d") for day in days]
    return "Planned for " + ", ".join(labels)


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
    toast_message: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    requirements = recipe_requirements(db, recipe)
    rows, unmatched = _recipe_rows(recipe, requirements)
    today, _ = _horizon()
    views = pantry_view(db, user.id)
    pantry = {v.ingredient.id: v for v in views}
    gone = probably_gone_ids(db, user.id, views, today)
    planned_on = db.scalars(
        select(MealPlanEntry.plan_date)
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.recipe_id == recipe.id,
            MealPlanEntry.plan_date >= today,
            MealPlanEntry.cooked_at.is_(None),
            MealPlanEntry.skipped_at.is_(None),
        )
        .order_by(MealPlanEntry.plan_date)
    ).all()
    latest = _latest_cook(db, user.id, recipe.id)
    live_cook = latest if latest is not None and latest.undone_at is None else None
    show_cooked_today = False
    if live_cook is not None:
        age = datetime.now(UTC) - _aware(live_cook.cooked_at)
        show_cooked_today = age < timedelta(days=1)
    return _render(
        request,
        "recipe_detail.html",
        planned_on=planned_on,
        status_code=status_code,
        recipe=recipe,
        rows=rows,
        unmatched=unmatched,
        pantry=pantry,
        gone=gone,
        cooked=cooked,
        notice=notice,
        match_error=match_error,
        saved=saved,
        toast_message=toast_message,
        today=today,
        days=[today + timedelta(days=offset) for offset in range(PLAN_DAYS)],
        cook_servings=_default_servings(recipe),
        max_servings=MAX_SERVINGS,
        live_cook=live_cook,
        show_cooked_today=show_cooked_today,
    )


@router.get("/recipes/new", response_class=HTMLResponse)
def new_recipe_form(
    request: Request,
    _user: User = Depends(auth.current_user),
) -> HTMLResponse:
    return _render_recipe_form(
        request, heading="Add a recipe", action="/recipes", cancel="/recipes"
    )


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
def recipe_detail(
    request: Request,
    recipe_id: int,
    cooked: int = 0,
    notice: str = "",
    saved: str = "",
    toast: str = "",
    planned: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    if toast == "Saved":
        toast_message = "Saved"
    elif toast == "Ignored":
        toast_message = "Ignored"
    elif planned:
        toast_message = _planned_message(planned)
    else:
        toast_message = ""
    return _recipe_page(
        request,
        db,
        user,
        recipe,
        cooked=bool(cooked),
        notice=notice,
        saved=saved.strip()[:80],
        toast_message=toast_message,
    )


@router.post("/recipes")
def create_recipe_from_form(
    request: Request,
    title: str = Form(""),
    servings: str = Form(""),
    minutes: str = Form(""),
    source_url: str = Form(""),
    ingredients: str = Form(""),
    method: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    parsed, error = _parse_recipe_form(title, servings, minutes, source_url, ingredients, method)
    if parsed is None:
        return _render_recipe_form(
            request,
            heading="Add a recipe",
            action="/recipes",
            cancel="/recipes",
            title=title,
            servings=servings,
            minutes=minutes,
            source_url=source_url,
            ingredients=ingredients,
            method=method,
            error=error,
            status_code=400,
        )
    recipe = create_manual_recipe(
        db,
        user.id,
        title=parsed.title,
        servings=parsed.servings,
        total_minutes=parsed.total_minutes,
        source_url=parsed.source_url,
        ingredient_lines=parsed.ingredient_lines,
        instructions=parsed.instructions,
    )
    return RedirectResponse(f"/recipes/{recipe.id}?toast=Saved", status_code=303)


@router.post("/recipes/import")
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

    recipe = _save_import(db, user, result.recipe, result.source, result.external_id)
    location = f"/recipes/{recipe.id}"
    if result.notice:
        location += "?notice=" + quote(result.notice)
    return RedirectResponse(location, status_code=303)


@router.post("/recipes/paste")
def paste_recipe(
    request: Request,
    text: str = Form(""),
    save: str = Form(""),
    edit: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    raw = parse_pasted_recipe(text)
    if not raw.is_usable:
        return _render(
            request,
            "import_result.html",
            error=(
                "No recipe found in that text. Start with the title, then the ingredient lines."
            ),
            text=text,
        )
    if edit == "1" and save != "1":
        return _render_recipe_form(
            request,
            heading="Add a recipe",
            action="/recipes",
            cancel="/recipes?import=1",
            title=raw.title,
            servings=str(raw.servings or 4),
            minutes="" if raw.total_minutes is None else str(raw.total_minutes),
            source_url=raw.source_url or "",
            ingredients="\n".join(raw.ingredients),
            method=_steps_text(raw.instructions),
        )
    if save != "1":
        return _render(request, "import_result.html", recipe=raw, text=text)
    # Pasted text is not a site import. MANUAL is that source; the column is a string.
    recipe = _save_import(db, user, raw, RecipeSource.MANUAL, None)
    return RedirectResponse(f"/recipes/{recipe.id}?toast=Saved", status_code=303)


def assign_named_match(
    db: Session,
    raw_text: str,
    name: str,
    lines: list[RecipeIngredient],
) -> CanonicalIngredient:
    """Point these lines at the food the cook named, and remember the alias.

    Exact catalog hits are reused. Anything else is stored under the name they
    typed — a fuzzy guess is how mirin became steak. The alias is the parsed
    ingredient name, not the whole line, so "2 tbsp mirin" does not become the key.
    """
    clean = name.strip()
    if not clean:
        raise NameMatchError("Enter the ingredient this line actually is.")
    if not lines:
        raise HTTPException(status_code=404, detail="No such line")

    from souschef.ingest.recipes.parsing import parse_line

    matcher = IngredientMatcher(db)
    match = matcher.match(clean)
    if match.method in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT):
        ingredient = db.get(CanonicalIngredient, match.ingredient_id)
    else:
        ingredient = CanonicalIngredient(
            name=clean,
            category=IngredientCategory.OTHER,
            base_unit=_base_unit_for_named_line(lines[0].unit or ""),
        )
        db.add(ingredient)
        db.flush()
    if ingredient is None:
        raise HTTPException(status_code=404, detail="No such ingredient")

    parsed_name = parse_line(raw_text).name.strip()
    if parsed_name and parsed_name.casefold() != raw_text.strip().casefold():
        matcher.learn(parsed_name, ingredient.id, source=AliasSource.MANUAL, is_receipt=False)
    for line in lines:
        line.ingredient_id = ingredient.id
        line.match_confidence = 1.0
    db.flush()
    return ingredient


@router.post("/recipes/{recipe_id}/match")
def correct_recipe_match(
    request: Request,
    recipe_id: int,
    raw_text: str = Form(...),
    name: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    lines = [line for line in recipe.ingredients if line.raw_text == raw_text]
    try:
        assign_named_match(db, raw_text, name, lines)
    except NameMatchError as exc:
        return _recipe_page(
            request,
            db,
            user,
            recipe,
            match_error=str(exc),
            status_code=400,
        )
    except UnitConversionError as exc:
        return _recipe_page(
            request,
            db,
            user,
            recipe,
            match_error=_kitchen_unit_message(exc, lines[0].unit or ""),
            status_code=400,
        )
    _sync_shopping(db, user.id)
    return RedirectResponse(
        f"/recipes/{recipe.id}?saved={quote(name.strip())}", status_code=303
    )


@router.post("/recipes/{recipe_id}/servings")
def remember_servings(
    recipe_id: int,
    servings: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    count = _whole_number(servings)
    if count is not None and 1 <= count <= MAX_SERVINGS:
        recipe.last_servings = count
    return RedirectResponse(f"/recipes/{recipe.id}", status_code=303)


@router.post("/recipes/{recipe_id}/ignore")
def ignore_recipe_line(
    recipe_id: int,
    raw_text: str = Form(...),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    lines = [line for line in recipe.ingredients if line.raw_text == raw_text]
    if not lines:
        raise HTTPException(status_code=404, detail="No such line")
    for line in lines:
        line.is_ignored = True
    db.flush()
    _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}?toast=Ignored", status_code=303)


@router.get("/recipes/{recipe_id}/edit", response_class=HTMLResponse)
def edit_recipe_form(
    request: Request,
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    return _render_recipe_form(
        request,
        heading="Edit recipe",
        action=f"/recipes/{recipe.id}",
        cancel=f"/recipes/{recipe.id}",
        **_form_fields(recipe),  # type: ignore[arg-type]
    )


@router.post("/recipes/{recipe_id}")
def save_recipe_edit(
    request: Request,
    recipe_id: int,
    title: str = Form(""),
    servings: str = Form(""),
    minutes: str = Form(""),
    source_url: str = Form(""),
    ingredients: str = Form(""),
    method: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    parsed, error = _parse_recipe_form(title, servings, minutes, source_url, ingredients, method)
    if parsed is None:
        return _render_recipe_form(
            request,
            heading="Edit recipe",
            action=f"/recipes/{recipe.id}",
            cancel=f"/recipes/{recipe.id}",
            title=title,
            servings=servings,
            minutes=minutes,
            source_url=source_url,
            ingredients=ingredients,
            method=method,
            error=error,
            status_code=400,
        )
    servings_changed = recipe.servings != parsed.servings
    lines_changed = edit_recipe(
        db,
        recipe,
        title=parsed.title,
        servings=parsed.servings,
        total_minutes=parsed.total_minutes,
        source_url=parsed.source_url,
        ingredient_lines=parsed.ingredient_lines,
        instructions=parsed.instructions,
    )
    if lines_changed or servings_changed:
        _sync_shopping(db, user.id)
    return RedirectResponse(f"/recipes/{recipe.id}?toast=Saved", status_code=303)


@router.post("/recipes/{recipe_id}/archive")
def archive_recipe(
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    recipe.is_archived = True
    return RedirectResponse(f"/recipes?archived={recipe.id}", status_code=303)


@router.post("/recipes/{recipe_id}/restore")
def restore_recipe(
    recipe_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    recipe = _owned(db, Recipe, recipe_id, user, "recipe")
    recipe.is_archived = False
    return RedirectResponse("/recipes?toast=Restored", status_code=303)


@router.get("/share")
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
