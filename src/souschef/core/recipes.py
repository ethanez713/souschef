"""Recipe persistence and requirement computation.

The important function here is :func:`recipe_requirements`, which converts a
recipe into "how much of each canonical ingredient, in base units". Everything
downstream -- suggestions, shopping lists, cooking -- is built on it, so it has
to be careful about the things that don't convert:

* vague amounts ("salt to taste") contribute nothing
* unconvertible units (a "cup" of something with no density) are reported as
  unknown rather than guessed at
* unmatched lines are surfaced, never silently dropped
* ignored lines (plain water, "to serve", or a cook's Ignore) are omitted from
  both the totals and the unmatched list

Reporting these instead of hiding them is what keeps a recipe from quietly
looking cookable when it isn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import physical_of
from souschef.core.matching import IngredientMatcher
from souschef.core.units import UnitConversionError, to_base
from souschef.ingest.recipes.jsonld import RawRecipe, unescape_text
from souschef.ingest.recipes.parsing import parse_all, parse_and_match
from souschef.logconfig import get_logger
from souschef.models import CanonicalIngredient, Recipe, RecipeIngredient, RecipeSource
from souschef.models.ingredient import AliasSource

log = get_logger("souschef.recipes")


@dataclass
class Requirement:
    ingredient: CanonicalIngredient
    quantity: float  # base units; 0.0 when the amount is vague
    is_staple: bool
    is_optional: bool
    is_vague: bool
    raw_text: str


@dataclass
class RequirementSet:
    requirements: list[Requirement] = field(default_factory=list)
    # Lines we could not resolve to a canonical ingredient.
    unmatched: list[str] = field(default_factory=list)
    # Matched, but the amount could not be converted to base units.
    unconvertible: list[str] = field(default_factory=list)

    def as_map(self, *, include_staples: bool = True) -> dict[int, float]:
        """Ingredient id -> total base-unit quantity, merging duplicate lines.

        Recipes routinely list the same ingredient twice ("2 tbsp oil, divided"
        then "1 tbsp oil for the pan"), so summing rather than overwriting is
        required for the totals to be right.
        """
        totals: dict[int, float] = {}
        for req in self.requirements:
            if not include_staples and req.is_staple:
                continue
            if req.quantity <= 0:
                continue
            totals[req.ingredient.id] = totals.get(req.ingredient.id, 0.0) + req.quantity
        return totals

    @property
    def needs_review(self) -> bool:
        return bool(self.unmatched or self.unconvertible)


def repair_stored_text(session: Session, recipe: Recipe) -> None:
    """Decode HTML entities already saved on a recipe.

    Import used to keep the source encoding, so an instruction read back as
    ``3&quot;``. Unescape is stable once the text is plain, so a later read
    does not write again.
    """
    changed = False
    for attr in ("title", "description", "instructions"):
        value = getattr(recipe, attr)
        if not isinstance(value, str):
            continue
        decoded = unescape_text(value)
        if decoded != value:
            setattr(recipe, attr, decoded)
            changed = True
    for line in recipe.ingredients:
        decoded = unescape_text(line.raw_text)
        if decoded is not None and decoded != line.raw_text:
            line.raw_text = decoded
            changed = True
    if changed:
        session.flush()


def rematch_unmatched(session: Session, recipe: Recipe) -> None:
    """Give stored lines the match the catalog can make now.

    Import matched each line once and saved the result. A later improvement to
    matching would otherwise never reach those rows. Only a line with no
    ingredient is linked, so a correction the user already made stays put, and
    nothing here is written back into the alias table. A missing amount is
    filled when a re-parse finds one; an amount already stored is left alone.
    """
    pending = [
        line
        for line in recipe.ingredients
        if line.ingredient_id is None or (line.quantity is None and line.quantity_is_vague)
    ]
    if not pending:
        return

    matcher = IngredientMatcher(session)
    changed = False
    for line in pending:
        parsed = parse_and_match(line.raw_text, matcher)
        was_unmatched = line.ingredient_id is None
        if was_unmatched and parsed.match and parsed.match.is_match:
            line.ingredient_id = parsed.match.ingredient_id
            line.match_confidence = parsed.match.confidence
            changed = True
        if line.quantity is None and parsed.quantity is not None and (
            was_unmatched or line.quantity_is_vague
        ):
            line.quantity = parsed.quantity
            line.unit = parsed.unit
            line.quantity_is_vague = parsed.quantity_is_vague
            changed = True

    if changed:
        session.flush()


def recipe_requirements(
    session: Session,
    recipe: Recipe,
    *,
    servings: int | None = None,
) -> RequirementSet:
    """What a recipe needs, scaled to the servings actually being cooked."""
    repair_stored_text(session, recipe)
    rematch_unmatched(session, recipe)
    scale = 1.0
    if servings and recipe.servings:
        scale = servings / recipe.servings

    result = RequirementSet()

    for line in recipe.ingredients:
        if line.is_ignored:
            continue
        if line.ingredient_id is None:
            result.unmatched.append(line.raw_text)
            continue

        ingredient = line.ingredient
        if ingredient is None:
            result.unmatched.append(line.raw_text)
            continue

        if line.quantity_is_vague or line.quantity is None:
            # Known ingredient, unknown amount. Recorded at zero so it still
            # counts toward "do I have this at all?" without polluting totals.
            result.requirements.append(
                Requirement(ingredient, 0.0, ingredient.is_staple, line.is_optional, True,
                            line.raw_text)
            )
            continue

        try:
            base = to_base(line.quantity * scale, line.unit, physical_of(ingredient))
        except UnitConversionError as exc:
            log.warning(
                "Recipe %d: %r needs %s but %r has no density or per-item weight (%s)",
                recipe.id, line.raw_text[:60], line.unit, ingredient.name, exc,
            )
            result.unconvertible.append(line.raw_text)
            result.requirements.append(
                Requirement(ingredient, 0.0, ingredient.is_staple, line.is_optional, True,
                            line.raw_text)
            )
            continue

        result.requirements.append(
            Requirement(ingredient, base, ingredient.is_staple, line.is_optional, False,
                        line.raw_text)
        )

    return result


def save_imported_recipe(
    session: Session,
    user_id: int,
    raw: RawRecipe,
    *,
    source: RecipeSource,
    external_id: str | None = None,
    learn_aliases: bool = True,
) -> Recipe:
    """Persist an imported recipe into one user's library.

    Re-importing the same source recipe updates that user's existing row rather
    than creating a duplicate. The de-duplication lookups are scoped to the
    owner: unscoped, a second user importing a recipe someone else already has
    would silently edit the first user's copy instead of getting their own.
    """
    existing = None
    if external_id:
        existing = session.scalar(
            select(Recipe).where(
                Recipe.user_id == user_id,
                Recipe.source == source,
                Recipe.external_id == external_id,
            )
        )
    if existing is None and raw.source_url:
        existing = session.scalar(
            select(Recipe).where(
                Recipe.user_id == user_id, Recipe.source_url == raw.source_url
            )
        )

    recipe = existing or Recipe(user_id=user_id)
    recipe.title = unescape_text(raw.title) or raw.title
    recipe.source = source
    recipe.source_url = raw.source_url
    recipe.external_id = external_id
    recipe.author = unescape_text(raw.author)
    recipe.description = unescape_text(raw.description)
    recipe.instructions = unescape_text(raw.instructions)
    recipe.image_url = raw.image_url
    recipe.servings = raw.servings or 4
    recipe.total_minutes = raw.total_minutes

    if existing is None:
        session.add(recipe)
    else:
        # Re-import replaces the ingredient lines wholesale. Deleting through
        # the relationship and re-adding below leaves the loaded collection
        # stale, so it is expired after the rebuild -- see the flush at the end.
        recipe.ingredients.clear()
    session.flush()

    from souschef.planning.suggest import is_ignorable

    matcher = IngredientMatcher(session)
    parsed_lines = parse_all(
        [unescape_text(item) or item for item in raw.ingredients], matcher
    )

    unmatched = [p.raw_text for p in parsed_lines if not (p.match and p.match.is_match)]
    if unmatched:
        # A gap in the catalog, not a transient failure: these lines are
        # invisible to pantry checks until the ingredient exists or is aliased.
        log.warning(
            "Recipe %r: %d of %d ingredient lines did not match the catalog: %s",
            raw.title, len(unmatched), len(parsed_lines), "; ".join(unmatched[:5]),
        )

    for position, parsed in enumerate(parsed_lines):
        match = parsed.match
        session.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                position=position,
                raw_text=parsed.raw_text,
                ingredient_id=match.ingredient_id if match and match.is_match else None,
                match_confidence=match.confidence if match else 0.0,
                quantity=parsed.quantity,
                unit=parsed.unit,
                preparation=parsed.preparation,
                is_optional=parsed.is_optional,
                is_ignored=is_ignorable(parsed.raw_text),
                quantity_is_vague=parsed.quantity_is_vague,
            )
        )

        # Feed strong matches back into the alias table so the vocabulary grows
        # with use. Weak matches are left alone -- learning a guess would make
        # the matcher confidently wrong later.
        if learn_aliases and match and match.is_auto and parsed.name:
            matcher.learn(parsed.name, match.ingredient_id, source=AliasSource.RECIPE)

    session.flush()
    # The new lines were added by id rather than through the relationship, so
    # the loaded collection is stale. Expiring it forces a reload on next
    # access; without this a re-imported recipe reads back as having no
    # ingredients, which scores as "you have everything" and is silently wrong.
    session.expire(recipe, ["ingredients"])
    return recipe
