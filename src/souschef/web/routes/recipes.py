"""Routes: recipes."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import pantry_view
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.recipes import recipe_requirements, save_imported_recipe
from souschef.core.units import UnitConversionError
from souschef.db import get_db
from souschef.ingest.recipes.importers import ImportError_, import_from_url
from souschef.models import CanonicalIngredient, Recipe, User
from souschef.models.ingredient import AliasSource, IngredientCategory
from souschef.planning.suggest import by_surface, suggest
from souschef.web import auth
from souschef.web.common import (
    PLAN_DAYS,
    _adhoc_units,
    _horizon,
    _is_htmx,
    _kitchen_unit_message,
    _owned,
    _render,
)

router = APIRouter()


@router.get("/recipes", response_class=HTMLResponse)
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
    catalog = db.scalars(select(CanonicalIngredient.name).order_by(CanonicalIngredient.name)).all()
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


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
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
        request,
        db,
        user,
        recipe,
        cooked=bool(cooked),
        notice=notice,
        saved=saved.strip()[:80],
    )


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

    recipe = save_imported_recipe(
        db, user.id, result.recipe, source=result.source, external_id=result.external_id
    )
    location = f"/recipes/{recipe.id}"
    if result.notice:
        location += "?notice=" + quote(result.notice)
    return RedirectResponse(location, status_code=303)


@router.post("/recipes/{recipe_id}/match")
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
            request,
            db,
            user,
            recipe,
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
            request,
            db,
            user,
            recipe,
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
    return RedirectResponse(f"/recipes/{recipe.id}?saved={quote(clean)}", status_code=303)


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
