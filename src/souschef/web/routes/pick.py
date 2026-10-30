"""Routes: ingredient and recipe pickers.

Empty queries list what this kitchen touched recently. A typed query ranks
exact, then prefix, then rapidfuzz over catalog names and aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from souschef.core.belief import aware
from souschef.core.inventory import pantry_view
from souschef.core.matching import normalize
from souschef.db import get_db
from souschef.models import (
    CanonicalIngredient,
    IngredientAlias,
    MealPlanEntry,
    PantryItem,
    Recipe,
    User,
)
from souschef.planning.next_step import readiness_chip
from souschef.planning.suggest import probably_gone_ids, score_recipe
from souschef.web import auth
from souschef.web.common import _render

router = APIRouter()

# A picker sheet shows a short list, not the catalog.
RESULT_LIMIT = 20
# Below this a rapidfuzz score is not a suggestion. Exact and prefix skip it.
FUZZY_CUTOFF = 70


@dataclass(frozen=True)
class PickRow:
    id: int
    name: str
    chip: str = ""
    base_unit: str = ""
    count_noun: str | None = None
    package_qty: float | None = None
    package_unit: str | None = None
    package_noun: str | None = None


@dataclass(frozen=True)
class _Rank:
    tier: int
    via_name: int
    score: float


def _phrases_for(db: Session) -> list[tuple[CanonicalIngredient, set[str]]]:
    ingredients = db.scalars(select(CanonicalIngredient)).all()
    phrases: dict[int, set[str]] = {}
    for ingredient in ingredients:
        keys = {normalize(ingredient.name)}
        if ingredient.plural_name:
            keys.add(normalize(ingredient.plural_name))
        keys.discard("")
        phrases[ingredient.id] = keys
    alias_rows = db.execute(
        select(IngredientAlias.ingredient_id, IngredientAlias.normalized_text)
    )
    for ingredient_id, text in alias_rows:
        if text and ingredient_id in phrases:
            phrases[ingredient_id].add(text)
    return [(ingredient, phrases[ingredient.id]) for ingredient in ingredients]


def _rank(name: str, phrases: set[str], needle: str) -> _Rank | None:
    name_key = normalize(name)
    keys = set(phrases)
    if name_key:
        keys.add(name_key)
    keys.discard("")
    if not keys:
        return None
    if name_key == needle:
        return _Rank(0, 0, 100)
    if needle in keys:
        return _Rank(0, 1, 100)
    name_prefix = bool(name_key) and name_key.startswith(needle)
    if name_prefix or any(key.startswith(needle) for key in keys):
        return _Rank(1, 0 if name_prefix else 1, 100)
    # "paste" finds "tomato paste": a later word starting with the query beats a fuzzy hit.
    if any(word.startswith(needle) for key in keys for word in key.split()[1:]):
        return _Rank(1, 2, 100)
    score = max(fuzz.WRatio(needle, key) for key in keys)
    if score >= FUZZY_CUTOFF:
        return _Rank(2, 1, float(score))
    return None


def _search_ingredients(
    db: Session, needle: str
) -> tuple[list[CanonicalIngredient], bool]:
    ranked: list[tuple[_Rank, str, CanonicalIngredient]] = []
    exact = False
    for ingredient, phrases in _phrases_for(db):
        rank = _rank(ingredient.name, phrases, needle)
        if rank is None:
            continue
        if rank.tier == 0:
            exact = True
        ranked.append((rank, ingredient.name, ingredient))
    ranked.sort(key=lambda item: (item[0].tier, item[0].via_name, -item[0].score, item[1]))
    return [ingredient for _, _, ingredient in ranked[:RESULT_LIMIT]], exact


def _ingredient_row(ingredient: CanonicalIngredient) -> PickRow:
    return PickRow(
        id=ingredient.id,
        name=ingredient.name,
        base_unit=ingredient.base_unit,
        count_noun=ingredient.count_noun,
        package_qty=ingredient.package_qty,
        package_unit=ingredient.package_unit,
        package_noun=getattr(ingredient, "package_noun", None),
    )


def _recent_ingredients(db: Session, user_id: int) -> list[PickRow]:
    items = db.scalars(
        select(PantryItem)
        .where(PantryItem.user_id == user_id)
        .options(selectinload(PantryItem.ingredient))
        .order_by(PantryItem.updated_at.desc(), PantryItem.id.desc())
        .limit(RESULT_LIMIT)
    ).all()
    return [_ingredient_row(item.ingredient) for item in items if item.ingredient is not None]


def _stamp(*values: datetime | None) -> datetime | None:
    stamps = [aware(value) for value in values if value is not None]
    return max(stamps) if stamps else None


def _recent_recipes(db: Session, user_id: int) -> list[Recipe]:
    recipes = db.scalars(
        select(Recipe)
        .where(Recipe.user_id == user_id, Recipe.is_archived.is_(False))
        .options(selectinload(Recipe.ingredients))
    ).all()
    planned: dict[int, datetime] = {}
    entries = db.scalars(
        select(MealPlanEntry).where(
            MealPlanEntry.user_id == user_id,
            MealPlanEntry.recipe_id.is_not(None),
        )
    ).all()
    for entry in entries:
        if entry.recipe_id is None:
            continue
        stamp = _stamp(entry.updated_at, entry.created_at)
        if stamp is None:
            continue
        current = planned.get(entry.recipe_id)
        if current is None or stamp > current:
            planned[entry.recipe_id] = stamp
    ranked: list[tuple[datetime, Recipe]] = []
    for recipe in recipes:
        stamp = _stamp(recipe.last_cooked_at, planned.get(recipe.id))
        if stamp is None:
            continue
        ranked.append((stamp, recipe))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [recipe for _, recipe in ranked[:RESULT_LIMIT]]


def _matching_recipes(db: Session, user_id: int, needle: str) -> list[Recipe]:
    recipes = db.scalars(
        select(Recipe)
        .where(Recipe.user_id == user_id, Recipe.is_archived.is_(False))
        .options(selectinload(Recipe.ingredients))
    ).all()
    ranked: list[tuple[_Rank, str, Recipe]] = []
    for recipe in recipes:
        rank = _rank(recipe.title, set(), needle)
        if rank is None:
            continue
        ranked.append((rank, recipe.title, recipe))
    ranked.sort(key=lambda item: (item[0].tier, item[0].via_name, -item[0].score, item[1]))
    return [recipe for _, _, recipe in ranked[:RESULT_LIMIT]]


def _recipe_rows(db: Session, user_id: int, recipes: list[Recipe]) -> list[PickRow]:
    if not recipes:
        return []
    today_views = pantry_view(db, user_id)
    today = date.today()
    gone = probably_gone_ids(db, user_id, today_views, today)
    pantry = {view.ingredient.id: view for view in today_views if view.ingredient.id not in gone}
    rows: list[PickRow] = []
    for recipe in recipes:
        suggestion = score_recipe(db, recipe, pantry, today=today)
        rows.append(
            PickRow(
                id=recipe.id,
                name=recipe.title,
                chip=readiness_chip(suggestion),
            )
        )
    return rows


def _fragment(
    request: Request,
    rows: list[PickRow],
    *,
    add_query: str = "",
    kind: str = "",
    asked: str = "",
) -> HTMLResponse:
    return _render(
        request,
        "pick_rows.html",
        rows=rows,
        add_query=add_query,
        kind=kind,
        asked=asked,
    )


@router.get("/pick/ingredient", response_class=HTMLResponse)
def pick_ingredient(
    request: Request,
    q: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    asked = q.strip()
    needle = normalize(q)
    if not needle:
        return _fragment(request, _recent_ingredients(db, user.id), kind="ingredient")
    found, exact = _search_ingredients(db, needle)
    return _fragment(
        request,
        [_ingredient_row(ingredient) for ingredient in found],
        add_query="" if exact else asked,
        kind="ingredient",
        asked=asked,
    )


@router.get("/pick/recipe", response_class=HTMLResponse)
def pick_recipe(
    request: Request,
    q: str = "",
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    asked = q.strip()
    needle = normalize(q)
    recipes = (
        _recent_recipes(db, user.id) if not needle else _matching_recipes(db, user.id, needle)
    )
    return _fragment(
        request, _recipe_rows(db, user.id, recipes), kind="recipe", asked=asked
    )
