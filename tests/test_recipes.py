"""Recipe import, requirement computation, suggestion and shopping-list tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import record_event
from souschef.core.recipes import recipe_requirements, save_imported_recipe
from souschef.ingest.recipes.jsonld import extract_recipe
from souschef.ingest.recipes.parsing import parse_line
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    Recipe,
    RecipeSource,
)
from souschef.planning.shopping import build_shopping_list, to_purchase_quantity
from souschef.planning.suggest import Surface, score_recipe, suggest

FIXTURE = Path(__file__).parent / "fixtures" / "recipe_jsonld.html"


@pytest.fixture
def recipe_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def imported(seeded_session: Session, recipe_html: str) -> Recipe:
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    assert raw is not None
    return save_imported_recipe(seeded_session, raw, source=RecipeSource.URL)


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None, name
    return found


def stock(session: Session, name: str, qty: float, unit: str | None = None) -> None:
    record_event(session, ing(session, name), reason=EventReason.PURCHASE, delta=qty, unit=unit)


# ---------------------------------------------------------------- JSON-LD


def test_extracts_recipe_from_graph(recipe_html: str) -> None:
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    assert raw is not None
    assert raw.title == "Skillet Chicken With Tomatoes"
    assert len(raw.ingredients) == 9
    assert raw.servings == 4
    assert raw.total_minutes == 45
    assert raw.author == "A Cook"
    assert raw.image_url == "https://example.com/img.jpg"
    assert "Season the chicken." in raw.instructions


def test_ignores_non_recipe_nodes(recipe_html: str) -> None:
    """The @graph also contains a WebPage node that must not be mistaken for a recipe."""
    raw = extract_recipe(recipe_html)
    assert raw is not None
    assert raw.title != "ignore me"


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no markup at all</body></html>",
        '<html><script type="application/ld+json">{ broken json</script></html>',
        '<html><script type="application/ld+json">{"@type":"Recipe"}</script></html>',
    ],
)
def test_unusable_pages_return_none(html: str) -> None:
    """Malformed input fails soft; a recipe with no ingredients is not usable."""
    assert extract_recipe(html) is None


# ---------------------------------------------------------------- parsing


def test_parses_quantity_unit_and_name() -> None:
    parsed = parse_line("1 1/2 pounds boneless, skinless chicken thighs")
    assert parsed.quantity == pytest.approx(1.5)
    assert parsed.unit == "lb"
    assert "chicken" in parsed.name.lower()
    assert not parsed.quantity_is_vague


def test_flags_vague_amounts_rather_than_inventing_numbers() -> None:
    for text in ("Kosher salt, to taste", "olive oil, for serving", "a pinch of saffron"):
        parsed = parse_line(text)
        assert parsed.quantity_is_vague, text
        assert parsed.is_optional, text


# ---------------------------------------------------------------- requirements


def test_requirements_convert_to_base_units(seeded_session: Session, imported: Recipe) -> None:
    requirements = recipe_requirements(seeded_session, imported)
    by_name = {r.ingredient.name: r for r in requirements.requirements}

    assert by_name["chicken thigh"].quantity == pytest.approx(680, rel=0.01)  # 1.5 lb
    assert by_name["olive oil"].quantity == pytest.approx(29.6, rel=0.02)  # 2 tbsp
    assert by_name["heavy cream"].quantity == pytest.approx(118, rel=0.02)  # 1/2 cup
    assert not requirements.unmatched
    assert not requirements.unconvertible


def test_requirements_scale_with_servings(seeded_session: Session, imported: Recipe) -> None:
    single = recipe_requirements(seeded_session, imported, servings=4)
    double = recipe_requirements(seeded_session, imported, servings=8)
    a = {r.ingredient.name: r.quantity for r in single.requirements}
    b = {r.ingredient.name: r.quantity for r in double.requirements}
    assert b["chicken thigh"] == pytest.approx(a["chicken thigh"] * 2)


def test_duplicate_lines_are_summed_not_overwritten(seeded_session: Session) -> None:
    """"2 tbsp oil, divided" plus "1 tbsp oil for the pan" is three tablespoons."""
    from souschef.ingest.recipes.jsonld import RawRecipe

    raw = RawRecipe(
        title="Oily thing",
        ingredients=["2 tablespoons olive oil", "1 tablespoon olive oil"],
        source_url="https://example.com/oil",
    )
    recipe = save_imported_recipe(seeded_session, raw, source=RecipeSource.URL)
    totals = recipe_requirements(seeded_session, recipe).as_map()
    oil_id = ing(seeded_session, "olive oil").id
    assert totals[oil_id] == pytest.approx(44.4, rel=0.02)  # 3 tbsp


def test_staples_can_be_excluded_from_totals(seeded_session: Session, imported: Recipe) -> None:
    requirements = recipe_requirements(seeded_session, imported)
    with_staples = requirements.as_map(include_staples=True)
    without = requirements.as_map(include_staples=False)
    assert ing(seeded_session, "olive oil").id in with_staples
    assert ing(seeded_session, "olive oil").id not in without


# ---------------------------------------------------------------- re-import


def test_reimport_updates_in_place_without_duplicating(
    seeded_session: Session, recipe_html: str
) -> None:
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    first = save_imported_recipe(seeded_session, raw, source=RecipeSource.URL)
    second = save_imported_recipe(seeded_session, raw, source=RecipeSource.URL)

    assert first.id == second.id
    assert len(seeded_session.scalars(select(Recipe)).all()) == 1


def test_reimported_recipe_keeps_its_ingredients(
    seeded_session: Session, recipe_html: str
) -> None:
    """Regression: rebuilding lines left the loaded collection empty.

    An empty ingredient list makes a recipe look perfectly cookable, so this
    failure was silent and would have put un-shoppable meals on the plan.
    """
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    save_imported_recipe(seeded_session, raw, source=RecipeSource.URL)
    again = save_imported_recipe(seeded_session, raw, source=RecipeSource.URL)

    assert len(again.ingredients) == 9
    assert recipe_requirements(seeded_session, again).requirements


def test_recipe_with_no_ingredients_is_never_cook_now(seeded_session: Session) -> None:
    """Absence of evidence is not evidence you have everything."""
    empty = Recipe(title="Mystery", source=RecipeSource.MANUAL, servings=4)
    seeded_session.add(empty)
    seeded_session.flush()

    suggestion = score_recipe(seeded_session, empty, {})
    assert suggestion.surface is not Surface.COOK_NOW
    assert suggestion.availability == 0.0


# ---------------------------------------------------------------- suggestion


def test_fully_stocked_recipe_is_cook_now(seeded_session: Session, imported: Recipe) -> None:
    stock(seeded_session, "chicken thigh", 1000)
    stock(seeded_session, "yellow onion", 3)
    stock(seeded_session, "garlic", 10)
    stock(seeded_session, "canned crushed tomatoes", 2)
    stock(seeded_session, "heavy cream", 500)
    stock(seeded_session, "spaghetti", 500)

    top = suggest(seeded_session)[0]
    assert top.surface == Surface.COOK_NOW
    assert top.missing == []
    assert top.reason == "You have everything"


def test_partially_stocked_recipe_lists_what_is_missing(
    seeded_session: Session, imported: Recipe
) -> None:
    stock(seeded_session, "yellow onion", 3)
    stock(seeded_session, "garlic", 10)
    stock(seeded_session, "heavy cream", 400)

    top = suggest(seeded_session)[0]
    assert top.surface == Surface.NEARLY_THERE
    assert {m.ingredient.name for m in top.missing} == {
        "chicken thigh", "canned crushed tomatoes", "spaghetti"
    }


def test_staples_never_count_as_missing(seeded_session: Session, imported: Recipe) -> None:
    """Nobody wants "you can't cook this, you need salt"."""
    stock(seeded_session, "chicken thigh", 1000)
    stock(seeded_session, "yellow onion", 3)
    stock(seeded_session, "garlic", 10)
    stock(seeded_session, "canned crushed tomatoes", 2)
    stock(seeded_session, "heavy cream", 500)
    stock(seeded_session, "spaghetti", 500)
    # Deliberately no olive oil or salt in the pantry at all.

    top = suggest(seeded_session)[0]
    assert top.surface == Surface.COOK_NOW


def test_expiring_ingredients_raise_a_recipe_up_the_list(
    seeded_session: Session, imported: Recipe
) -> None:
    stock(seeded_session, "chicken thigh", 1000)
    stock(seeded_session, "yellow onion", 3)
    stock(seeded_session, "garlic", 10)
    stock(seeded_session, "canned crushed tomatoes", 2)
    stock(seeded_session, "heavy cream", 500)
    stock(seeded_session, "spaghetti", 500)

    baseline = suggest(seeded_session)[0].score

    cream = ing(seeded_session, "heavy cream")
    cream.pantry_item.expires_on = date.today() + timedelta(days=2)
    seeded_session.flush()

    boosted = suggest(seeded_session)[0]
    assert boosted.score > baseline
    assert "heavy cream" in [i.name for i in boosted.uses_expiring]
    assert "before it goes off" in boosted.reason


def test_recently_cooked_recipes_are_penalized(seeded_session: Session, imported: Recipe) -> None:
    stock(seeded_session, "chicken thigh", 1000)
    stock(seeded_session, "yellow onion", 3)
    stock(seeded_session, "garlic", 10)
    stock(seeded_session, "canned crushed tomatoes", 2)
    stock(seeded_session, "heavy cream", 500)
    stock(seeded_session, "spaghetti", 500)

    baseline = suggest(seeded_session)[0].score
    imported.last_cooked_at = datetime.now(UTC) - timedelta(days=1)
    seeded_session.flush()

    assert suggest(seeded_session)[0].score < baseline


# ---------------------------------------------------------------- shopping


def test_purchase_quantity_rounds_up_to_a_real_package(seeded_session: Session) -> None:
    """Needing 680 g of chicken means buying one 907 g pack, not "680 g of chicken"."""
    chicken = ing(seeded_session, "chicken thigh")
    qty, unit, note = to_purchase_quantity(chicken, 680)
    assert qty == 1
    assert "907" in unit
    assert note and "680" in note

    # Two packs once the need exceeds one.
    qty, _, _ = to_purchase_quantity(chicken, 1500)
    assert qty == 2


def test_countable_items_are_expressed_as_counts(seeded_session: Session) -> None:
    onion = ing(seeded_session, "yellow onion")
    qty, unit, _ = to_purchase_quantity(onion, 3)
    assert (qty, unit) == (3.0, "count")


def test_shopping_list_covers_the_gap_only(seeded_session: Session, imported: Recipe) -> None:
    stock(seeded_session, "yellow onion", 3)
    stock(seeded_session, "garlic", 10)
    stock(seeded_session, "heavy cream", 400)

    today = date.today()
    seeded_session.add(
        MealPlanEntry(plan_date=today, slot=MealSlot.DINNER, recipe_id=imported.id, servings=4)
    )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, covers_from=today, covers_to=today + timedelta(days=7)
    )
    labels = {item.label for item in shopping.items}

    assert labels == {"chicken thigh", "canned crushed tomatoes", "spaghetti"}
    # Things already in the pantry, and staples, stay off the list.
    assert "yellow onion" not in labels
    assert "olive oil" not in labels


def test_shopping_list_records_why_each_item_is_there(
    seeded_session: Session, imported: Recipe
) -> None:
    today = date.today()
    seeded_session.add(
        MealPlanEntry(plan_date=today, slot=MealSlot.DINNER, recipe_id=imported.id, servings=4)
    )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, covers_from=today, covers_to=today + timedelta(days=7)
    )
    assert all(item.driven_by == imported.title for item in shopping.items)
    assert all(item.aisle for item in shopping.items)


def test_cooked_meals_do_not_drive_shopping(seeded_session: Session, imported: Recipe) -> None:
    today = date.today()
    seeded_session.add(
        MealPlanEntry(
            plan_date=today,
            slot=MealSlot.DINNER,
            recipe_id=imported.id,
            servings=4,
            cooked_at=datetime.now(UTC),
        )
    )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, covers_from=today, covers_to=today + timedelta(days=7)
    )
    assert shopping.items == []
