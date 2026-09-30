"""Score recipes against what's actually in the kitchen.

Answers the question the user starts from: *given what I have, what should I
cook this week?* Recipes are sorted into three surfaces because the decision is
different in each case:

* **Cook now** -- nothing missing. Tonight's answer.
* **Nearly there** -- a small number of cheap, common things missing. These are
  what a weekly shop is for.
* **Reach** -- possible, but a real shopping trip.

Scoring deliberately favours food you already know. A suggester that surfaces
novel recipes every week is a discovery tool; this is a planning tool, and the
user asked for a library of recipes they are familiar with. Variety comes from
a recency penalty rather than from novelty-seeking.

Ingredients about to expire get a bonus, which is the one place the app is
opinionated about *what* to cook rather than merely what is possible. Borrowed
from Grocy's "due score", and it is the difference between a planner and a
list-maker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from souschef.core.inventory import PantryView, pantry_view
from souschef.core.recipes import RequirementSet, recipe_requirements
from souschef.models import CanonicalIngredient, Recipe

# How close to expiry an ingredient must be to count as "use me first".
EXPIRY_HORIZON_DAYS = 5
# A recipe cooked within this window is penalized, for variety's sake.
RECENCY_WINDOW_DAYS = 21

# Scoring weights. Kept as named constants so the ranking can be re-tuned
# against real use, which is the only way to know if it feels right.
W_AVAILABILITY = 1.0
W_EXPIRING = 0.35
W_FAMILIARITY = 0.20
W_FAVORITE = 0.15
W_RATING = 0.10
W_RECENCY_PENALTY = 0.30


class Surface(StrEnum):
    COOK_NOW = "cook_now"
    NEARLY_THERE = "nearly_there"
    REACH = "reach"


@dataclass
class MissingIngredient:
    ingredient: CanonicalIngredient
    needed: float
    have: float
    confidence: float

    @property
    def shortfall(self) -> float:
        return max(0.0, self.needed - self.have)

    @property
    def is_uncertain(self) -> bool:
        """We may well have it; the level is just too stale to rely on."""
        return self.confidence < 0.6 and self.have > 0


@dataclass
class Suggestion:
    recipe: Recipe
    score: float
    surface: Surface
    missing: list[MissingIngredient] = field(default_factory=list)
    uses_expiring: list[CanonicalIngredient] = field(default_factory=list)
    availability: float = 0.0
    # Non-optional recipe lines that never resolved to a catalog ingredient.
    unidentified: int = 0

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def reason(self) -> str:
        """A short human explanation of why this was suggested."""
        if self.uses_expiring:
            names = ", ".join(i.name for i in self.uses_expiring[:2])
            base = f"Uses {names} before it goes off"
        elif self.surface == Surface.COOK_NOW:
            base = "You have everything"
        elif self.missing_count == 1:
            base = f"Just needs {self.missing[0].ingredient.name}"
        else:
            base = f"Needs {self.missing_count} things"
        if self.unidentified:
            noun = "line" if self.unidentified == 1 else "lines"
            return f"{base} · {self.unidentified} {noun} not identified"
        return base


def nonoptional_unmatched(recipe: Recipe, requirements: RequirementSet) -> list[str]:
    """Unmatched lines the recipe actually needs.

    ``RequirementSet.unmatched`` is every unresolved line, garnish included.
    Optional extras stay out of readiness and the shopping list.
    """
    pool = list(requirements.unmatched)
    kept: list[str] = []
    for line in recipe.ingredients:
        if line.raw_text not in pool:
            continue
        pool.remove(line.raw_text)
        if line.ingredient_id is not None or line.is_optional:
            continue
        kept.append(line.raw_text)
    kept.extend(pool)
    return kept


def _expiring_soon(view: PantryView, today: date) -> bool:
    if view.item.expires_on is None or view.quantity <= 0:
        return False
    return 0 <= (view.item.expires_on - today).days <= EXPIRY_HORIZON_DAYS


def score_recipe(
    session: Session,
    recipe: Recipe,
    pantry: dict[int, PantryView],
    *,
    today: date | None = None,
    servings: int | None = None,
) -> Suggestion:
    """Score one recipe against the current pantry."""
    today = today or datetime.now(UTC).date()
    requirements = recipe_requirements(session, recipe, servings=servings)

    considered = 0
    satisfied = 0.0
    missing: list[MissingIngredient] = []
    uses_expiring: list[CanonicalIngredient] = []

    for req in requirements.requirements:
        # Staples are assumed present and optional items don't gate a recipe;
        # counting either would make everything look unmakeable.
        if req.is_staple or req.is_optional:
            continue

        considered += 1
        view = pantry.get(req.ingredient.id)
        have = view.quantity if view else 0.0
        confidence = view.confidence if view else 0.0

        if view is not None and _expiring_soon(view, today):
            uses_expiring.append(req.ingredient)

        if req.quantity <= 0:
            # Known ingredient, vague amount: presence is all we can check.
            if have > 0:
                satisfied += 1.0
            else:
                missing.append(MissingIngredient(req.ingredient, 0.0, 0.0, confidence))
            continue

        if have >= req.quantity:
            satisfied += 1.0
        elif have > 0:
            # Partial credit: having most of what you need is meaningfully
            # closer than having none, and this keeps ranking smooth.
            satisfied += have / req.quantity
            missing.append(MissingIngredient(req.ingredient, req.quantity, have, confidence))
        else:
            missing.append(MissingIngredient(req.ingredient, req.quantity, 0.0, confidence))

    # A recipe with nothing to check is not a recipe you can definitely cook --
    # it is a recipe we failed to understand (import broke, every line was
    # unmatched, or the ingredient list is empty). Scoring that as fully
    # available would promote the least-trustworthy entries straight to
    # "Cook now", so absence of evidence is scored as absence of availability.
    availability = (satisfied / considered) if considered else 0.0

    score = W_AVAILABILITY * availability
    if uses_expiring:
        score += W_EXPIRING * min(1.0, len(uses_expiring) / 2.0)
    if recipe.times_cooked:
        score += W_FAMILIARITY * min(1.0, recipe.times_cooked / 5.0)
    if recipe.is_favorite:
        score += W_FAVORITE
    if recipe.rating:
        score += W_RATING * (recipe.rating / 5.0)

    if recipe.last_cooked_at is not None:
        last = recipe.last_cooked_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        days_ago = (datetime.now(UTC) - last).days
        if days_ago < RECENCY_WINDOW_DAYS:
            score -= W_RECENCY_PENALTY * (1 - days_ago / RECENCY_WINDOW_DAYS)

    if not considered:
        surface = Surface.REACH
    elif not missing:
        surface = Surface.COOK_NOW
    elif len(missing) <= 3:
        surface = Surface.NEARLY_THERE
    else:
        surface = Surface.REACH

    return Suggestion(
        recipe=recipe,
        score=round(score, 4),
        surface=surface,
        missing=missing,
        uses_expiring=uses_expiring,
        availability=round(availability, 4),
        unidentified=len(nonoptional_unmatched(recipe, requirements)),
    )


def suggest(
    session: Session,
    user_id: int,
    *,
    limit: int = 20,
    today: date | None = None,
    include_archived: bool = False,
) -> list[Suggestion]:
    """Rank one user's library against their pantry."""
    pantry = {v.ingredient.id: v for v in pantry_view(session, user_id)}

    query = (
        select(Recipe)
        .options(selectinload(Recipe.ingredients))
        .where(Recipe.user_id == user_id)
    )
    if not include_archived:
        query = query.where(Recipe.is_archived.is_(False))

    suggestions = [
        score_recipe(session, recipe, pantry, today=today)
        for recipe in session.scalars(query)
    ]
    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions[:limit]


def by_surface(suggestions: list[Suggestion]) -> dict[Surface, list[Suggestion]]:
    grouped: dict[Surface, list[Suggestion]] = {s: [] for s in Surface}
    for suggestion in suggestions:
        grouped[suggestion.surface].append(suggestion)
    return grouped
