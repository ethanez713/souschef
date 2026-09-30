"""Matcher regression tests.

The cases here are the ones that were actually wrong during development, not a
sampling of easy wins. The canned-vs-fresh pairs matter most: getting those
backwards silently records the wrong food in the pantry, which is worse than
failing to match at all, because nothing surfaces it for review.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.matching import (
    AUTO_ACCEPT,
    IngredientMatcher,
    MatchMethod,
    head_segment,
    normalize,
    strip_noise,
)
from souschef.models import CanonicalIngredient
from souschef.models.ingredient import AliasSource

# (text, expected canonical name, is_receipt)
RECIPE_CASES = [
    ("extra-virgin olive oil, divided", "olive oil"),
    ("2 cloves garlic, minced", "garlic"),
    ("1 large yellow onion, finely diced", "yellow onion"),
    ("freshly ground black pepper", "black pepper"),
    ("4 boneless skinless chicken breasts", "chicken breast"),
    ("flat-leaf parsley, roughly chopped", "fresh parsley"),
    ("3 tablespoons unsalted butter, softened", "butter"),
    ("kosher salt and freshly ground pepper", "kosher salt"),
    ("grated Parmigiano-Reggiano", "parmesan cheese"),
    ("1/2 cup heavy cream", "heavy cream"),
    ("2 cups all-purpose flour", "all-purpose flour"),
    ("2 tablespoons whole wheat flour", "whole wheat flour"),
    ("1 cup whole milk", "whole milk"),
    ("1/2 cup brown sugar, packed", "brown sugar"),
    ("1 cup frozen peas", "frozen peas"),
    ("2 cups chicken stock", "chicken stock"),
    ("1 tablespoon sesame oil", "sesame oil"),
    ("2 tablespoons soy sauce", "soy sauce"),
    ("1 bunch scallions, thinly sliced", "scallion"),
    ("juice of 1 lemon", "lemon"),
    ("1 pound ground beef", "ground beef"),
    ("1 large red pepper", "bell pepper"),
    ("1 teaspoon red pepper flakes", "red pepper flakes"),
    # Journal strings. Each one was "Couldn't identify" after 74918fc.
    ("1 cup long-grain white rice (uncooked)", "white rice"),
    ("1 medium white or yellow onion, finely diced", "yellow onion"),
    ("1 tbsp cornflour / corn starch", "cornstarch"),
    ("1/2 tsp baking soda / bi-carb", "baking soda"),
    ("Dash of white pepper ((or black))", "black pepper"),
    ("2 large egg yolks", "eggs"),
    ("1 (28-ounce) can crushed, or finely diced tomatoes", "canned crushed tomatoes"),
    ("6 oz bittersweet chocolate or semisweet chocolate chips", "chocolate chips"),
    ("2-3 tsp coconut sugar (or other sweetener)", "coconut sugar"),
    ("4 (6- to 8-oz.) boneless, skinless chicken breasts", "chicken breast"),
    ("Lemon wedges, for serving", "lemon"),
    ("1 can puréed tomatoes", "canned crushed tomatoes"),
    ("2 tsp paprika", "paprika"),
]

RECEIPT_CASES = [
    ("GV EVOO 25.5Z          8.47 F", "olive oil"),
    ("BNLS SKLS CHKN BRST  2.34 lb  11.68 F", "chicken breast"),
    ("ORG YEL ONION 3LB  4.99", "yellow onion"),
    ("GRND BF 80/20        6.49 F", "ground beef"),
    ("SHRD MOZZ CHZ 8OZ    3.29 F", "mozzarella cheese"),
    ("LG EGGS GRADE A 12CT 4.19", "eggs"),
    ("HVY WHIP CRM 16OZ    4.49", "heavy cream"),
    ("KRAFT PARM CHZ 8OZ   5.99 F", "parmesan cheese"),
    ("BANANAS  1.24 lb  0.71", "banana"),
    ("ORG BABY SPINACH 5OZ 3.99", "spinach"),
    ("PASTA ORG SPAGHETTI", "spaghetti"),
    ("SALAD SPINACH BABY 6OZ", "spinach"),
    ("FZN ORGANIC SPINACH", "frozen spinach"),
    ("ORANGE ORIGINAL FROM CONCENTRA", "orange"),
    ("CLIF CRUNCHY PEANUT BUTTER", "peanut butter"),
    ("PASTA SAUCE ORG VODKA", "tomato sauce"),
    ("PASTA SAUCE BOLOGNESE 24 OZ", "tomato sauce"),
    ("FZN ORGANIC PEAS", "frozen peas"),
    ("IZN ORGANIC PEAS 661", "frozen peas"),
    # One slipped letter, the confusions that show up on thermal paper.
    ("PASIA ORG SPAGHETTI", "spaghetti"),
    ("PASTA ORG SPAGHETII", "spaghetti"),
    ("PASIA ORG SPACHETTI", "spaghetti"),
    ("FZN URGANIC SPINACH", "frozen spinach"),
    ("BRUSSELS SPROUIS 1LB", "brussels sprouts"),
    ("BRUSSELSPROUIS 1LB", "brussels sprouts"),
    ("BRUSSEISPROUTS1LB", "brussels sprouts"),
]

# The pairs that distinguish a preserved product from its fresh counterpart.
# These share a token set and are separable only by word order and by refusing
# to strip catalog-meaningful words.
CANNED_VS_FRESH_CASES = [
    ("1 (28-ounce) can crushed tomatoes", "canned crushed tomatoes"),
    ("one 14.5 ounce can diced tomatoes", "canned diced tomatoes"),
    ("1 can diced tomatoes, drained", "canned diced tomatoes"),
    ("2 large tomatoes, diced", "tomato"),
    ("3 tomatoes, roughly chopped", "tomato"),
    ("1 can black beans, rinsed and drained", "black beans"),
    ("1 can of tuna, drained", "canned tuna"),
]


@pytest.fixture
def matcher(seeded_session: Session) -> IngredientMatcher:
    return IngredientMatcher(seeded_session)


@pytest.mark.parametrize(("text", "expected"), RECIPE_CASES)
def test_matches_recipe_lines(matcher: IngredientMatcher, text: str, expected: str) -> None:
    result = matcher.match(text)
    assert result.name == expected, f"{text!r} -> {result.name!r}"
    assert result.confidence >= AUTO_ACCEPT


@pytest.mark.parametrize(("text", "expected"), RECEIPT_CASES)
def test_matches_receipt_lines(matcher: IngredientMatcher, text: str, expected: str) -> None:
    result = matcher.match(text, is_receipt=True)
    assert result.name == expected, f"{text!r} -> {result.name!r}"


@pytest.mark.parametrize(("text", "expected"), CANNED_VS_FRESH_CASES)
def test_preserved_and_fresh_forms_do_not_collapse(
    matcher: IngredientMatcher, text: str, expected: str
) -> None:
    """Canned and fresh are not substitutable; conflating them corrupts the pantry."""
    result = matcher.match(text)
    assert result.name == expected, f"{text!r} -> {result.name!r}"


def test_catalog_words_survive_noise_stripping(matcher: IngredientMatcher) -> None:
    """A word used in any ingredient name is identity-bearing and must not be stripped."""
    protected = matcher._protected
    for word in ("crushed", "diced", "ground", "whole", "frozen", "brown", "heavy"):
        assert word in protected
        assert strip_noise(f"{word} tomatoes", protected).startswith(word)

    # ...while words the catalog never uses are still stripped as noise.
    stripped = strip_noise("finely chopped fresh parsley, roughly torn", protected)
    for generic in ("finely", "roughly", "torn"):
        assert generic not in stripped
    assert "parsley" in stripped


def test_head_segment_splits_product_from_preparation() -> None:
    assert head_segment("2 large tomatoes, diced") == "2 large tomatoes"
    assert head_segment("1 can diced tomatoes, drained") == "1 can diced tomatoes"
    # No comma: the whole line is the product.
    assert head_segment("1 (28-ounce) can crushed tomatoes") == "1 (28-ounce) can crushed tomatoes"
    # A leading comma would leave nothing useful, so fall back to the full line.
    assert head_segment(", drained") == ", drained"


def test_unmatchable_text_returns_no_match(matcher: IngredientMatcher) -> None:
    for text in ("", "   ", "xyzzy quux fnord"):
        assert not matcher.match(text).is_match


@pytest.mark.parametrize(
    "text",
    [
        "2 Tbsp mirin",
        "1 Tbsp sake",
        "ground coffee",
        "ginger snaps",
        "deviled egg",
        "1 cup bean sprouts",
        "1 tbsp Oyster Sauce",
        "1 tbsp Chinese Cooking Wine",
        "2-3 fresh green chilies",
    ],
)
def test_a_shared_word_is_not_another_food(matcher: IngredientMatcher, text: str) -> None:
    """token_set_ratio scores a shared modifier as a perfect match."""
    assert not matcher.match(text).is_match


def test_receipt_shorthand_does_not_match_on_one_shared_word(matcher: IngredientMatcher) -> None:
    assert not matcher.match("JOE COFFEE MEDIUM ROAST GROUND", is_receipt=True).is_match
    assert not matcher.match("PRG CHED SC", is_receipt=True).is_match
    assert not matcher.match("RIDGE CUT POTATO CHIPS", is_receipt=True).is_match
    for furniture in ("TOTAL", "TAX", "PURCHASE APPROVED", "ARC:00"):
        assert not matcher.match(furniture, is_receipt=True).is_match, furniture


def test_white_onion_is_not_silently_yellow(matcher: IngredientMatcher) -> None:
    """The onion line names both. A line that only says white onion does not."""
    assert not matcher.match("1 medium white onion").is_match


def test_a_typed_name_is_exact(matcher: IngredientMatcher, seeded_session: Session) -> None:
    """'This is' / 'Wrong match' must not fuzzy back onto the row being corrected."""
    paprika = matcher.match_exact("paprika")
    smoked = matcher.match_exact("smoked paprika")
    assert paprika.name == "paprika"
    assert paprika.method == MatchMethod.NAME_EXACT
    assert smoked.name == "smoked paprika"
    assert smoked.ingredient_id != paprika.ingredient_id
    assert not matcher.match_exact("no such spice").is_match

    # A seed alias left over from when smoked paprika was an alias of paprika.
    paprika_id = paprika.ingredient_id
    assert paprika_id is not None
    matcher.learn("smoked paprika", paprika_id, source=AliasSource.SEED)
    seeded_session.flush()
    assert matcher.match_exact("smoked paprika").name == "smoked paprika"
    assert IngredientMatcher(seeded_session).match_exact("smoked paprika").name == "smoked paprika"


def _alias_owners(session: Session, text: str) -> set[int]:
    from souschef.models.ingredient import IngredientAlias

    return set(
        session.scalars(
            select(IngredientAlias.ingredient_id).where(IngredientAlias.normalized_text == text)
        )
    )


def test_reseeding_prunes_a_dropped_seed_alias_and_keeps_a_taught_one(
    matcher: IngredientMatcher, seeded_session: Session
) -> None:
    """An upgraded database still holds seed aliases the seed file no longer lists."""
    from souschef.core.seeding import seed_ingredients

    paprika_id = matcher.match_exact("paprika").ingredient_id
    matcher.learn("smoked paprika", paprika_id, source=AliasSource.SEED)
    matcher.learn("pimenton", paprika_id, source=AliasSource.MANUAL)
    seeded_session.flush()

    seed_ingredients(seeded_session)

    assert paprika_id not in _alias_owners(seeded_session, "smoked paprika")
    assert _alias_owners(seeded_session, "pimenton") == {paprika_id}

    # A correction the user actually made does outrank the catalog name.
    matcher.learn("smoked paprika", paprika_id, source=AliasSource.MANUAL)
    seeded_session.flush()
    assert matcher.match_exact("smoked paprika").name == "paprika"
    assert IngredientMatcher(seeded_session).match_exact("smoked paprika").name == "paprika"


def test_a_recipe_learned_alias_does_not_outrank_the_catalog_name(
    matcher: IngredientMatcher, seeded_session: Session
) -> None:
    paprika_id = seeded_session.scalar(
        select(CanonicalIngredient.id).where(CanonicalIngredient.name == "paprika")
    )
    assert paprika_id is not None
    matcher.learn("smoked paprika", paprika_id, source=AliasSource.RECIPE)
    seeded_session.flush()
    assert matcher.match_exact("smoked paprika").name == "smoked paprika"


def test_frozen_abbreviation_matches_frozen_spinach(matcher: IngredientMatcher) -> None:
    result = matcher.match("FZN ORGANIC SPINACH", is_receipt=True)
    assert result.name == "frozen spinach"


def test_learning_an_alias_makes_the_next_match_exact(
    matcher: IngredientMatcher, seeded_session: Session
) -> None:
    """A correction must stick -- this is what stops receipts being tedious."""
    weird = "SLIVERY GRN THING 4.99"
    assert matcher.match(weird, is_receipt=True).name != "scallion"

    scallion_id = matcher._by_name["scallion"]
    matcher.learn(weird, scallion_id, source=AliasSource.MANUAL, is_receipt=True)
    seeded_session.flush()

    again = matcher.match(weird, is_receipt=True)
    assert again.name == "scallion"
    assert again.confidence == 1.0

    # And it survives a fresh matcher built from the database.
    assert IngredientMatcher(seeded_session).match(weird, is_receipt=True).name == "scallion"


def test_learning_is_idempotent(matcher: IngredientMatcher, seeded_session: Session) -> None:
    ing_id = matcher._by_name["olive oil"]
    first = matcher.learn("fancy oil", ing_id, source=AliasSource.MANUAL)
    seeded_session.flush()
    second = matcher.learn("fancy oil", ing_id, source=AliasSource.MANUAL)
    seeded_session.flush()

    assert first.id == second.id
    assert second.times_confirmed == 2


def test_normalize_is_stable() -> None:
    assert normalize("Extra-Virgin Olive Oil!") == "extra virgin olive oil"
    assert normalize("  MULTIPLE   SPACES  ") == "multiple spaces"
    assert normalize("") == ""
