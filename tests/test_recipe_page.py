"""What the recipe page tells you about a dish before you cook or plan it."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.models import (
    CanonicalIngredient,
    MealPlanEntry,
    MealSlot,
    Recipe,
    RecipeIngredient,
    RecipeSource,
)
from souschef.web.common import _fractions


def _recipe(session: Session, user_id: int, line: str, name: str, grams: float) -> Recipe:
    ingredient = session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == name)
    )
    assert ingredient is not None, name
    recipe = Recipe(user_id=user_id, title="Test dish", servings=2, source=RecipeSource.MANUAL)
    session.add(recipe)
    session.flush()
    session.add(
        RecipeIngredient(
            recipe_id=recipe.id, position=0, raw_text=line,
            ingredient_id=ingredient.id, quantity=grams, unit="g",
        )
    )
    session.commit()
    return recipe


@pytest.mark.parametrize(
    ("typed", "shown"),
    [
        ("1 1/2 lb chicken thighs", "1½ lb chicken thighs"),
        ("1/2 cup heavy cream", "½ cup heavy cream"),
        ("2 3/4 cups flour", "2¾ cups flour"),
        ("1 (14 oz) can tomatoes", "1 (14 oz) can tomatoes"),
        ("open 24/7", "open 24/7"),
        ("1/5 of the dough", "1/5 of the dough"),
    ],
)
def test_typed_fractions_show_as_glyphs(typed: str, shown: str) -> None:
    assert _fractions(typed) == shown


def test_recipe_page_shows_fraction_glyphs_but_posts_the_raw_line(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _recipe(seeded_session, web_user.id, "1 1/2 lb chicken thigh", "chicken thigh", 680)

    page = signed_in.get(f"/recipes/{recipe.id}").text

    assert "1½ lb chicken thigh" in page
    assert 'value="1 1/2 lb chicken thigh"' in page


def test_recipe_page_says_when_it_is_planned(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _recipe(seeded_session, web_user.id, "200 g rice", "white rice", 200)
    today = date.today()
    seeded_session.add_all(
        MealPlanEntry(
            user_id=web_user.id, recipe_id=recipe.id, plan_date=today + timedelta(days=offset),
            slot=MealSlot.DINNER, servings=2,
        )
        for offset in (1, -2)
    )
    seeded_session.commit()

    page = signed_in.get(f"/recipes/{recipe.id}").text

    assert "planned tomorrow" in page
    assert "Yesterday" not in page

