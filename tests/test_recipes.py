"""Recipe import, requirement computation, suggestion and shopping-list tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import get_or_create_item, record_event
from souschef.core.matching import IngredientMatcher
from souschef.core.recipes import recipe_requirements, save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe, extract_recipe
from souschef.ingest.recipes.parsing import parse_and_match, parse_line
from souschef.ingest.recipes.paste import parse_pasted_recipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    Recipe,
    RecipeIngredient,
    RecipeSource,
)
from souschef.planning.shopping import (
    build_shopping_list,
    collect_requirements,
    to_purchase_quantity,
    unnamed_recipe_lines,
)
from souschef.planning.suggest import Surface, is_ignorable, score_recipe, suggest

FIXTURE = Path(__file__).parent / "fixtures" / "recipe_jsonld.html"
UNMATCHED_LINES = Path(__file__).parent / "fixtures" / "unmatched_lines.txt"


@pytest.fixture
def recipe_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def imported(seeded_session: Session, recipe_html: str, owner: int) -> Recipe:
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    assert raw is not None
    return save_imported_recipe(seeded_session, owner, raw, source=RecipeSource.URL)


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None, name
    return found


def stock(
    session: Session, owner: int, name: str, qty: float, unit: str | None = None
) -> None:
    record_event(
        session, owner, ing(session, name), reason=EventReason.PURCHASE, delta=qty, unit=unit
    )


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


def test_imported_instructions_unescape_html_entities() -> None:
    """Recipetineats stores the inch mark as ``&quot;`` and the page showed it."""
    html = """
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "Recipe",
     "name": "Noodles &amp; Co",
     "recipeIngredient": ["1 onion"],
     "recipeInstructions": "Cut into 7cm (3&quot;) pieces"}
    </script>
    """
    raw = extract_recipe(html, source_url="https://example.com/noodles")
    assert raw is not None
    assert raw.title == "Noodles & Co"
    assert raw.instructions == 'Cut into 7cm (3") pieces'


def test_scraper_fallback_unescapes_html_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    from souschef.ingest.recipes.importers import _scrape_fallback

    class _Scraper:
        def title(self) -> str:
            return "A &amp; B"

        def ingredients(self) -> list[str]:
            return ["1 &amp; 2 onions"]

        def instructions(self) -> str:
            return "Cut into 7cm (3&amp;quot;) pieces"

        def yields(self) -> str:
            return "4"

        def total_time(self) -> None:
            return None

        def image(self) -> None:
            return None

        def author(self) -> None:
            return None

        def description(self) -> str:
            return "3&amp;quot; deep"

    monkeypatch.setattr("recipe_scrapers.scrape_html", lambda *_args, **_kwargs: _Scraper())
    raw = _scrape_fallback("<html></html>", "https://example.com/noodles")
    assert raw is not None
    assert raw.title == "A & B"
    assert raw.ingredients == ["1 & 2 onions"]
    assert raw.instructions == 'Cut into 7cm (3") pieces'
    assert raw.description == '3" deep'


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


def _pasted(name: str) -> RawRecipe:
    return parse_pasted_recipe((FIXTURE.parent / name).read_text(encoding="utf-8"))


def test_pasted_recipe_with_headings_and_colons() -> None:
    raw = _pasted("paste_headings.txt")
    assert raw.title == "Skillet Chili"
    assert raw.ingredients == [
        "1 lb ground beef",
        "1 onion, chopped",
        "2 cloves garlic",
        "1 can diced tomatoes",
        "1 tbsp chili powder",
    ]
    assert raw.instructions is not None
    assert raw.instructions.split("\n\n") == [
        "Brown the beef with the onion and garlic.",
        "Stir in the tomatoes and chili powder.",
        "Simmer until thickened.",
    ]
    assert raw.servings == 4
    assert raw.total_minutes == 75


def test_pasted_recipe_without_an_ingredients_heading_uses_bullets() -> None:
    raw = _pasted("paste_bullets.txt")
    assert raw.title == "Tomato Salad"
    assert raw.ingredients == [
        "2 tomatoes",
        "1 cucumber",
        "2 tbsp olive oil",
        "a pinch of salt",
    ]
    assert raw.instructions is not None
    assert raw.instructions.split("\n\n") == [
        "Slice the tomatoes and cucumber.",
        "Toss with the oil and salt.",
    ]
    assert raw.servings == 2


def test_pasted_recipe_reads_servings_from_a_makes_line() -> None:
    raw = _pasted("paste_makes.txt")
    assert raw.title == "Chocolate Chip Cookies"
    assert len(raw.ingredients) == 4
    assert raw.instructions is not None
    assert len(raw.instructions.split("\n\n")) == 4
    assert raw.servings == 12
    assert raw.total_minutes == 45


def test_paste_strips_list_numbers_but_keeps_decimal_quantities() -> None:
    raw = parse_pasted_recipe(
        "Pancakes\n\n"
        "## INGREDIENTS:\n"
        "1. 2 cups flour\n"
        "2. 2.5 tbsp sugar\n"
        "3. 1 egg\n"
        "\n"
        "Method:\n"
        "1. Mix the flour and the sugar.\n"
    )
    assert raw.ingredients == ["2 cups flour", "2.5 tbsp sugar", "1 egg"]
    assert raw.instructions == "Mix the flour and the sugar."


def test_paste_keeps_the_first_servings_line() -> None:
    raw = parse_pasted_recipe("Soup\n\nServes: 4\n\n- 1 onion\n\nSimmer.\n\nServes: 8\n")
    assert raw.servings == 4
    assert raw.instructions == "Simmer."
    assert raw.ingredients == ["1 onion"]


def test_paste_keeps_a_cook_sentence_instead_of_reading_it_as_a_time() -> None:
    raw = parse_pasted_recipe("Pasta\n\n- 1 pound spaghetti\n\nCook for 10 minutes.\n")
    assert raw.total_minutes is None
    assert raw.instructions == "Cook for 10 minutes."
    assert raw.ingredients == ["1 pound spaghetti"]


def test_pasted_total_time_wins_over_prep_and_cook() -> None:
    raw = parse_pasted_recipe(
        "Soup\n\nPrep time: 10 minutes\nCook time: 2 hours\nTotal time: 50 minutes\n\n"
        "- 1 onion\n\nSimmer.\n"
    )
    assert raw.total_minutes == 50
    assert raw.servings is None
    assert raw.ingredients == ["1 onion"]


@pytest.mark.parametrize("text", ["", "   \n\n", "Tuesday plan\n\nBuy milk.\n"])
def test_pasted_text_without_ingredients_is_not_a_recipe(text: str) -> None:
    assert parse_pasted_recipe(text).is_usable is False


def test_jsonld_prefers_the_recipe_at_the_requested_url() -> None:
    html = """
    <script type="application/ld+json">
    {"@graph":[
      {"@type":"Recipe","name":"Other dish","url":"https://example.com/other",
       "recipeIngredient":["1 egg"]},
      {"@type":"Recipe","name":"The one asked for","url":"https://www.example.com/asked/",
       "recipeIngredient":["2 eggs"]}
    ]}
    </script>
    """
    raw = extract_recipe(html, source_url="https://example.com/asked?utm=1")
    assert raw is not None
    assert raw.title == "The one asked for"


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


def test_a_count_with_a_size_range_stays_a_count() -> None:
    parsed = parse_line("4 (6- to 8-oz.) boneless, skinless chicken breasts")
    assert parsed.quantity == 4
    assert parsed.unit == "count"
    assert not parsed.quantity_is_vague


def test_a_parenthetical_metric_weight_is_the_amount() -> None:
    cups = parse_line("3 cups (360g) all-purpose flour")
    assert cups.quantity == pytest.approx(360)
    assert cups.unit == "g"
    mixed = parse_line("1½ cups plus 1 Tbsp. (200 g) all-purpose flour")
    assert mixed.quantity == pytest.approx(200)
    assert mixed.unit == "g"
    # The ounces are the size of the can, not a second amount to shop for.
    canned = parse_line("1 (28-ounce) can crushed tomatoes")
    assert canned.quantity == 1
    assert canned.unit == "can"


def test_lemon_wedges_for_serving_are_lemon_and_still_vague(seeded_session: Session) -> None:
    parsed = parse_and_match("Lemon wedges, for serving", IngredientMatcher(seeded_session))
    assert parsed.match is not None
    assert parsed.match.name == "lemon"
    assert parsed.quantity_is_vague
    assert parsed.is_optional


def test_counted_chicken_converts_at_a_typical_breast(
    seeded_session: Session, owner: int
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        owner,
        RawRecipe(
            title="Tuscan chicken",
            ingredients=["4 (6- to 8-oz.) boneless, skinless chicken breasts"],
            servings=4,
        ),
        source=RecipeSource.MANUAL,
    )
    requirements = recipe_requirements(seeded_session, recipe)
    assert requirements.requirements[0].ingredient.name == "chicken breast"
    assert requirements.requirements[0].quantity == pytest.approx(792)
    assert not requirements.requirements[0].is_vague
    assert recipe.ingredients[0].quantity == 4
    assert recipe.ingredients[0].unit == "count"


def test_imported_flour_uses_the_stated_grams(seeded_session: Session, owner: int) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        owner,
        RawRecipe(
            title="Bread",
            ingredients=[
                "3 cups (360g) all-purpose flour",
                "1½ cups plus 1 Tbsp. (200 g) all-purpose flour",
            ],
            servings=4,
        ),
        source=RecipeSource.MANUAL,
    )
    by_text = {line.raw_text: line for line in recipe.ingredients}
    assert by_text["3 cups (360g) all-purpose flour"].quantity == pytest.approx(360)
    assert by_text["1½ cups plus 1 Tbsp. (200 g) all-purpose flour"].quantity == pytest.approx(200)
    totals = recipe_requirements(seeded_session, recipe).as_map()
    flour = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "all-purpose flour")
    )
    assert flour is not None
    assert totals[flour.id] == pytest.approx(560)


def test_stored_html_entities_are_repaired_on_read(
    seeded_session: Session, owner: int
) -> None:
    recipe = Recipe(
        user_id=owner,
        title="Cut &amp; fold",
        source=RecipeSource.MANUAL,
        servings=4,
        instructions="Cut into 7cm (3&quot;) pieces",
        description="A 3&amp;quot; piece",
    )
    recipe.ingredients.append(
        RecipeIngredient(position=0, raw_text="1 &quot;knob&quot; of ginger", quantity=1)
    )
    seeded_session.add(recipe)
    seeded_session.flush()

    recipe_requirements(seeded_session, recipe)
    assert recipe.title == "Cut & fold"
    assert recipe.instructions == 'Cut into 7cm (3") pieces'
    assert recipe.description == 'A 3" piece'
    assert recipe.ingredients[0].raw_text == '1 "knob" of ginger'

    recipe_requirements(seeded_session, recipe)
    assert recipe.instructions == 'Cut into 7cm (3") pieces'
    assert recipe.ingredients[0].raw_text == '1 "knob" of ginger'


def test_save_unescapes_recipe_text_that_arrived_encoded(
    seeded_session: Session, owner: int
) -> None:
    saved = save_imported_recipe(
        seeded_session,
        owner,
        RawRecipe(
            title="Noodles &amp; Co",
            ingredients=["1 onion"],
            instructions="Cut into 7cm (3&quot;) pieces",
        ),
        source=RecipeSource.MANUAL,
    )
    assert saved.title == "Noodles & Co"
    assert saved.instructions == 'Cut into 7cm (3") pieces'


def test_stored_unmatched_lines_resolve_when_the_recipe_is_read(
    seeded_session: Session, owner: int
) -> None:
    recipe = Recipe(
        user_id=owner, title="Leftover rice", source=RecipeSource.MANUAL, servings=4
    )
    recipe.ingredients.append(
        RecipeIngredient(
            position=0,
            raw_text="1 cup long-grain white rice (uncooked)",
            quantity=1,
            unit="cup",
        )
    )
    seeded_session.add(recipe)
    seeded_session.flush()

    requirements = recipe_requirements(seeded_session, recipe)
    assert not requirements.unmatched
    assert requirements.requirements[0].ingredient.name == "white rice"
    assert recipe.ingredients[0].quantity == 1
    assert recipe.ingredients[0].unit == "cup"


def test_rematch_does_not_override_a_chosen_ingredient(
    seeded_session: Session, owner: int
) -> None:
    oil = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "olive oil")
    )
    assert oil is not None
    recipe = Recipe(user_id=owner, title="Linked", source=RecipeSource.MANUAL, servings=4)
    recipe.ingredients.append(
        RecipeIngredient(
            position=0,
            raw_text="1 cup long-grain white rice (uncooked)",
            ingredient_id=oil.id,
            quantity=1,
            unit="cup",
        )
    )
    seeded_session.add(recipe)
    seeded_session.flush()

    recipe_requirements(seeded_session, recipe)
    assert recipe.ingredients[0].ingredient_id == oil.id


def test_a_vague_stored_amount_picks_up_parenthetical_grams(
    seeded_session: Session, owner: int
) -> None:
    flour = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "all-purpose flour")
    )
    salt = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "kosher salt")
    )
    assert flour is not None and salt is not None
    recipe = Recipe(user_id=owner, title="Refill", source=RecipeSource.MANUAL, servings=4)
    recipe.ingredients.append(
        RecipeIngredient(
            position=0,
            raw_text="3 cups (360g) all-purpose flour",
            ingredient_id=flour.id,
            quantity=None,
            quantity_is_vague=True,
        )
    )
    recipe.ingredients.append(
        RecipeIngredient(
            position=1,
            raw_text="Kosher salt, to taste",
            ingredient_id=salt.id,
            quantity=None,
            quantity_is_vague=True,
        )
    )
    seeded_session.add(recipe)
    seeded_session.flush()

    requirements = recipe_requirements(seeded_session, recipe)
    flour_line, salt_line = recipe.ingredients
    assert flour_line.ingredient_id == flour.id
    assert flour_line.quantity == pytest.approx(360)
    assert flour_line.unit == "g"
    assert not flour_line.quantity_is_vague
    assert salt_line.quantity is None
    assert salt_line.quantity_is_vague
    by_name = {req.ingredient.name: req for req in requirements.requirements}
    assert by_name["all-purpose flour"].quantity == pytest.approx(360)
    assert by_name["kosher salt"].is_vague


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


def test_duplicate_lines_are_summed_not_overwritten(seeded_session: Session, owner: int) -> None:
    """"2 tbsp oil, divided" plus "1 tbsp oil for the pan" is three tablespoons."""
    from souschef.ingest.recipes.jsonld import RawRecipe

    raw = RawRecipe(
        title="Oily thing",
        ingredients=["2 tablespoons olive oil", "1 tablespoon olive oil"],
        source_url="https://example.com/oil",
    )
    recipe = save_imported_recipe(seeded_session, owner, raw, source=RecipeSource.URL)
    totals = recipe_requirements(seeded_session, recipe).as_map()
    oil_id = ing(seeded_session, "olive oil").id
    assert totals[oil_id] == pytest.approx(44.4, rel=0.02)  # 3 tbsp


def test_staples_can_be_excluded_from_totals(seeded_session: Session, imported: Recipe) -> None:
    requirements = recipe_requirements(seeded_session, imported)
    with_staples = requirements.as_map(include_staples=True)
    without = requirements.as_map(include_staples=False)
    assert ing(seeded_session, "olive oil").id in with_staples
    assert ing(seeded_session, "olive oil").id not in without


def test_reimport_updates_in_place_without_duplicating(
    seeded_session: Session, recipe_html: str,
    owner: int) -> None:
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    first = save_imported_recipe(seeded_session, owner, raw, source=RecipeSource.URL)
    second = save_imported_recipe(seeded_session, owner, raw, source=RecipeSource.URL)

    assert first.id == second.id
    assert len(seeded_session.scalars(select(Recipe)).all()) == 1


def test_reimported_recipe_keeps_its_ingredients(
    seeded_session: Session, recipe_html: str,
    owner: int) -> None:
    """Regression: rebuilding lines left the loaded collection empty.

    An empty ingredient list makes a recipe look perfectly cookable, so this
    failure was silent and would have put un-shoppable meals on the plan.
    """
    raw = extract_recipe(recipe_html, source_url="https://example.com/r/1")
    save_imported_recipe(seeded_session, owner, raw, source=RecipeSource.URL)
    again = save_imported_recipe(seeded_session, owner, raw, source=RecipeSource.URL)

    assert len(again.ingredients) == 9
    assert recipe_requirements(seeded_session, again).requirements


def test_recipe_with_no_ingredients_is_never_cook_now(seeded_session: Session, owner: int) -> None:
    """Absence of evidence is not evidence you have everything."""
    empty = Recipe(user_id=owner, title="Mystery", source=RecipeSource.MANUAL, servings=4)
    seeded_session.add(empty)
    seeded_session.flush()

    suggestion = score_recipe(seeded_session, empty, {})
    assert suggestion.surface is not Surface.COOK_NOW
    assert suggestion.availability == 0.0


def test_fully_stocked_recipe_is_cook_now(
    seeded_session: Session, imported: Recipe, owner: int
) -> None:
    stock(seeded_session, owner, "chicken thigh", 1000)
    stock(seeded_session, owner, "yellow onion", 3)
    stock(seeded_session, owner, "garlic", 10)
    stock(seeded_session, owner, "canned crushed tomatoes", 2)
    stock(seeded_session, owner, "heavy cream", 500)
    stock(seeded_session, owner, "spaghetti", 500)

    top = suggest(seeded_session, owner)[0]
    assert top.surface == Surface.COOK_NOW
    assert top.missing == []
    assert top.reason == "You have everything"


def test_partially_stocked_recipe_lists_what_is_missing(
    seeded_session: Session, imported: Recipe,
    owner: int) -> None:
    stock(seeded_session, owner, "yellow onion", 3)
    stock(seeded_session, owner, "garlic", 10)
    stock(seeded_session, owner, "heavy cream", 400)

    top = suggest(seeded_session, owner)[0]
    assert top.surface == Surface.NEARLY_THERE
    assert top.missing == []
    assert {item.name for item in top.to_check} == {
        "chicken thigh", "canned crushed tomatoes", "spaghetti"
    }


def test_staples_never_count_as_missing(
    seeded_session: Session, imported: Recipe, owner: int
) -> None:
    """Nobody wants "you can't cook this, you need salt"."""
    stock(seeded_session, owner, "chicken thigh", 1000)
    stock(seeded_session, owner, "yellow onion", 3)
    stock(seeded_session, owner, "garlic", 10)
    stock(seeded_session, owner, "canned crushed tomatoes", 2)
    stock(seeded_session, owner, "heavy cream", 500)
    stock(seeded_session, owner, "spaghetti", 500)
    # Deliberately no olive oil or salt in the pantry at all.

    top = suggest(seeded_session, owner)[0]
    assert top.surface == Surface.COOK_NOW


def test_expiring_ingredients_raise_a_recipe_up_the_list(
    seeded_session: Session, imported: Recipe,
    owner: int) -> None:
    stock(seeded_session, owner, "chicken thigh", 1000)
    stock(seeded_session, owner, "yellow onion", 3)
    stock(seeded_session, owner, "garlic", 10)
    stock(seeded_session, owner, "canned crushed tomatoes", 2)
    stock(seeded_session, owner, "heavy cream", 500)
    stock(seeded_session, owner, "spaghetti", 500)

    baseline = suggest(seeded_session, owner)[0].score

    cream = ing(seeded_session, "heavy cream")
    get_or_create_item(seeded_session, owner, cream).expires_on = (
        date.today() + timedelta(days=2)
    )
    seeded_session.flush()

    boosted = suggest(seeded_session, owner)[0]
    assert boosted.score > baseline
    assert "heavy cream" in [i.name for i in boosted.uses_expiring]
    assert "before it goes off" in boosted.reason


def test_recently_cooked_recipes_are_penalized(
    seeded_session: Session, imported: Recipe, owner: int
) -> None:
    stock(seeded_session, owner, "chicken thigh", 1000)
    stock(seeded_session, owner, "yellow onion", 3)
    stock(seeded_session, owner, "garlic", 10)
    stock(seeded_session, owner, "canned crushed tomatoes", 2)
    stock(seeded_session, owner, "heavy cream", 500)
    stock(seeded_session, owner, "spaghetti", 500)

    baseline = suggest(seeded_session, owner)[0].score
    imported.last_cooked_at = datetime.now(UTC) - timedelta(days=1)
    seeded_session.flush()

    assert suggest(seeded_session, owner)[0].score < baseline


def test_purchase_quantity_rounds_up_to_a_real_package(seeded_session: Session) -> None:
    """Needing 680 g of chicken means buying one 907 g pack, not "680 g of chicken"."""
    chicken = ing(seeded_session, "chicken thigh")
    qty, unit, note = to_purchase_quantity(chicken, 680)
    assert qty == 1
    # The package size stays on the ingredient; the list formats it for whoever is reading.
    assert unit == "x"
    assert note is None

    # Two packs once the need exceeds one.
    qty, _, _ = to_purchase_quantity(chicken, 1500)
    assert qty == 2


def test_countable_items_are_expressed_as_counts(seeded_session: Session) -> None:
    onion = ing(seeded_session, "yellow onion")
    qty, unit, _ = to_purchase_quantity(onion, 3)
    assert (qty, unit) == (3.0, "count")


def test_shopping_list_covers_the_gap_only(
    seeded_session: Session, imported: Recipe, owner: int
) -> None:
    stock(seeded_session, owner, "yellow onion", 3)
    stock(seeded_session, owner, "garlic", 10)
    stock(seeded_session, owner, "heavy cream", 400)
    stock(seeded_session, owner, "chicken thigh", 0)
    stock(seeded_session, owner, "canned crushed tomatoes", 0)
    stock(seeded_session, owner, "spaghetti", 0)

    today = date.today()
    seeded_session.add(
        MealPlanEntry(
            user_id=owner,
            plan_date=today,
            slot=MealSlot.DINNER,
            recipe_id=imported.id,
            servings=4,
        )
    )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, owner, covers_from=today, covers_to=today + timedelta(days=7)
    )
    labels = {item.label for item in shopping.items}

    assert labels == {"chicken thigh", "canned crushed tomatoes", "spaghetti"}
    # Things already in the pantry, and staples, stay off the list.
    assert "yellow onion" not in labels
    assert "olive oil" not in labels


def test_a_matched_line_with_no_amount_asks_for_one_package(
    seeded_session: Session, owner: int
) -> None:
    fish_sauce = ing(seeded_session, "fish sauce")
    stock(seeded_session, owner, "fish sauce", 0)
    parsley = ing(seeded_session, "fresh parsley")
    recipe = Recipe(user_id=owner, title="Sauce", source=RecipeSource.MANUAL, servings=2)
    seeded_session.add(recipe)
    seeded_session.flush()
    seeded_session.add_all([
        RecipeIngredient(
            recipe_id=recipe.id, position=0, raw_text="fish sauce",
            ingredient_id=fish_sauce.id, quantity=None, quantity_is_vague=True, is_optional=False,
        ),
        RecipeIngredient(
            recipe_id=recipe.id, position=1, raw_text="parsley for garnish",
            ingredient_id=parsley.id, quantity=None, quantity_is_vague=True, is_optional=True,
        ),
    ])
    today = date.today()
    seeded_session.add(
        MealPlanEntry(
            user_id=owner, plan_date=today, slot=MealSlot.DINNER,
            recipe_id=recipe.id, servings=2,
        )
    )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, owner, covers_from=today, covers_to=today
    )
    by_label = {item.label: item for item in shopping.items}
    assert "fish sauce" in by_label
    assert by_label["fish sauce"].purchase_note == "amount wasn't on the recipe"
    assert by_label["fish sauce"].purchase_qty == 1
    assert "fresh parsley" not in by_label


def test_a_real_quantity_wins_over_an_unspecified_one(
    seeded_session: Session, owner: int
) -> None:
    fish_sauce = ing(seeded_session, "fish sauce")
    stock(seeded_session, owner, "fish sauce", 0)
    recipe = Recipe(user_id=owner, title="Two lines", source=RecipeSource.MANUAL, servings=1)
    seeded_session.add(recipe)
    seeded_session.flush()
    seeded_session.add_all([
        RecipeIngredient(
            recipe_id=recipe.id, position=0, raw_text="fish sauce",
            ingredient_id=fish_sauce.id, quantity=None, quantity_is_vague=True, is_optional=False,
        ),
        RecipeIngredient(
            recipe_id=recipe.id, position=1, raw_text="2 tbsp fish sauce",
            ingredient_id=fish_sauce.id, quantity=2, unit="tbsp", quantity_is_vague=False,
            is_optional=False,
        ),
    ])
    today = date.today()
    seeded_session.add(
        MealPlanEntry(
            user_id=owner, plan_date=today, slot=MealSlot.DINNER,
            recipe_id=recipe.id, servings=1,
        )
    )
    seeded_session.flush()
    shopping = build_shopping_list(
        seeded_session, owner, covers_from=today, covers_to=today
    )
    item = next(row for row in shopping.items if row.label == "fish sauce")
    assert item.purchase_note != "amount wasn't on the recipe"
    assert item.needed_qty > 0


def test_shopping_list_records_why_each_item_is_there(
    seeded_session: Session, imported: Recipe,
    owner: int) -> None:
    today = date.today()
    seeded_session.add(
        MealPlanEntry(
            user_id=owner,
            plan_date=today,
            slot=MealSlot.DINNER,
            recipe_id=imported.id,
            servings=4,
        )
    )
    for line in imported.ingredients:
        if line.ingredient is not None and not line.ingredient.is_staple:
            record_event(
                seeded_session,
                owner,
                line.ingredient,
                reason=EventReason.MANUAL,
                level=0,
            )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, owner, covers_from=today, covers_to=today + timedelta(days=7)
    )
    assert shopping.items
    assert all(item.driven_by == imported.title for item in shopping.items)
    assert all(item.aisle for item in shopping.items)


def test_cooked_meals_do_not_drive_shopping(
    seeded_session: Session, imported: Recipe, owner: int
) -> None:
    today = date.today()
    seeded_session.add(
        MealPlanEntry(
            user_id=owner,
            plan_date=today,
            slot=MealSlot.DINNER,
            recipe_id=imported.id,
            servings=4,
            cooked_at=datetime.now(UTC),
        )
    )
    seeded_session.flush()

    shopping = build_shopping_list(
        seeded_session, owner, covers_from=today, covers_to=today + timedelta(days=7)
    )
    assert shopping.items == []


def _allow_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from souschef.config import get_settings

    monkeypatch.setattr(get_settings(), "no_external", False)


def test_importer_identifies_itself_and_notices_a_redirect(
    monkeypatch: pytest.MonkeyPatch, seeded_session: Session, owner: int
) -> None:
    from souschef.ingest.recipes import importers

    seen: dict[str, str] = {}

    class Response:
        status_code = 200
        content = b"x"
        url = "https://example.com/landed"
        text = """<script type="application/ld+json">
        {"@type":"Recipe","name":"Landed page","recipeIngredient":["1 egg"],
         "url":"https://example.com/landed"}
        </script>"""

    def fake_get(url: str, headers: dict[str, str], **kwargs: object) -> Response:
        seen["ua"] = headers["User-Agent"]
        return Response()

    monkeypatch.setattr(importers.httpx, "get", fake_get)
    _allow_network(monkeypatch)
    result = importers.import_from_url(seeded_session, owner, "https://example.com/typed")
    assert seen["ua"].startswith("SousChef/")
    assert "Chrome" not in seen["ua"]
    assert result.recipe.title == "Landed page"
    assert result.notice is not None
    assert "https://example.com/landed" in result.notice


def test_a_refusal_blames_the_cookie_only_for_nyt(monkeypatch: pytest.MonkeyPatch) -> None:
    from souschef.ingest.recipes.importers import ImportError_, fetch_html

    class Response:
        status_code = 403
        content = b""
        text = ""
        url = "https://www.seriouseats.com/recipe"

    monkeypatch.setattr(
        "souschef.ingest.recipes.importers.httpx.get", lambda *args, **kwargs: Response()
    )
    _allow_network(monkeypatch)
    with pytest.raises(ImportError_, match="That site blocks automated imports"):
        fetch_html("https://www.seriouseats.com/recipe")


def test_unmatched_fixture_leaves_at_most_four_lines_needing_naming(
    seeded_session: Session, web_user, signed_in
) -> None:
    """Prices, notes and "or" alternatives match; water and "to serve" are ignored."""
    lines = UNMATCHED_LINES.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(title="Fixture stew", ingredients=lines, servings=4),
        source=RecipeSource.MANUAL,
    )
    for line in recipe.ingredients:
        assert line.is_ignored is is_ignorable(line.raw_text)

    needs = [
        line for line in recipe.ingredients if not line.is_ignored and line.ingredient_id is None
    ]
    assert len(needs) <= 4
    by_raw = {line.raw_text: line for line in recipe.ingredients}
    assert by_raw[lines[1]].ingredient.name == "ground beef"
    assert by_raw[lines[3]].ingredient.name == "table salt"
    assert by_raw[lines[6]].ingredient.name == "beef stock"
    assert by_raw[lines[8]].ingredient.name == "marjoram"
    assert by_raw[lines[9]].ingredient.name == "kidney beans"
    assert by_raw[lines[13]].ingredient.name == "Chinese cooking wine"
    assert by_raw[lines[14]].ingredient.name == "oyster sauce"
    assert by_raw[lines[15]].ingredient.name == "white pepper"

    requirements = recipe_requirements(seeded_session, recipe)
    ignored = {line.raw_text for line in recipe.ingredients if line.is_ignored}
    assert not ignored & set(requirements.unmatched)
    assert not ignored & {req.raw_text for req in requirements.requirements}
    assert len(requirements.unmatched) == len(needs)

    today = date.today()
    entry = MealPlanEntry(
        user_id=web_user.id,
        plan_date=today,
        slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    bought = {
        line.ingredient.name
        for line in collect_requirements(seeded_session, [entry]).values()
    }
    assert "ground beef" in bought
    assert "kidney beans" in bought
    assert "oyster sauce" in bought
    assert "Chinese cooking wine" in bought
    assert "sour cream" not in bought
    assert "white rice" not in bought
    unnamed = unnamed_recipe_lines(seeded_session, web_user.id, today, today)
    assert len(unnamed) <= 4
    assert not any(text in ignored for _recipe_id, _title, text in unnamed)

    page = signed_in.get(f"/recipes/{recipe.id}")
    assert page.status_code == 200
    assert page.text.count('<span class="badge warn">Needs naming</span>') == len(needs)
    for line in recipe.ingredients:
        assert line.raw_text in page.text
    shop = signed_in.get("/shopping")
    assert shop.status_code == 200
    for text in ignored:
        assert text not in shop.text
    for line in needs:
        assert line.raw_text in shop.text


def test_rate_limiting_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from souschef.ingest.recipes.importers import ImportError_, fetch_html

    class Response:
        status_code = 429
        content = b""
        text = ""
        url = "https://example.com/r"

    monkeypatch.setattr(
        "souschef.ingest.recipes.importers.httpx.get", lambda *args, **kwargs: Response()
    )
    _allow_network(monkeypatch)
    with pytest.raises(ImportError_, match="rate-limiting"):
        fetch_html("https://example.com/r")
