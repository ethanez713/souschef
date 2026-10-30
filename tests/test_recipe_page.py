"""What the recipe page tells you about a dish before you cook or plan it."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    IngredientAlias,
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


def _saved(session: Session, user_id: int, title: str, ingredients: list[str], **kwargs) -> Recipe:
    recipe = save_imported_recipe(
        session,
        user_id,
        RawRecipe(
            title=title,
            ingredients=ingredients,
            servings=kwargs.get("servings", 4),
            instructions=kwargs.get("instructions"),
            source_url=f"https://example.com/{title.replace(' ', '-').lower()}",
        ),
        source=RecipeSource.URL,
    )
    session.commit()
    return recipe


def test_recipe_page_offers_one_cook_and_one_plan(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _saved(
        seeded_session,
        web_user.id,
        "Paprika Chicken",
        ["2 tsp smoked paprika", "1 pound chicken thighs"],
        instructions="Toast the paprika.\n\nSear the chicken.",
    )

    page = signed_in.get(f"/recipes/{recipe.id}").text

    assert page.count(">Cook</a>") == 1
    assert page.count(">Plan</button>") == 1
    assert "Wrong match" not in page
    assert "I cooked this" not in page
    assert 'class="list-row-title">2 tsp smoked paprika</span>' in page
    assert '<ol class="method">' in page
    assert ">Edit</a>" in page
    assert ">Archive</button>" in page
    assert "Which day?" in page


def test_servings_stepper_is_remembered_on_the_plan(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _saved(seeded_session, web_user.id, "Remember Servings", ["1 pound spaghetti"])

    posted = signed_in.post(f"/recipes/{recipe.id}/servings", data={"servings": "3"})

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/recipes/{recipe.id}"
    seeded_session.expire_all()
    assert seeded_session.get(Recipe, recipe.id).last_servings == 3
    page = signed_in.get(f"/recipes/{recipe.id}").text
    assert 'name="servings" value="3"' in page

    for bad in ("0", "51", "nope"):
        assert signed_in.post(
            f"/recipes/{recipe.id}/servings", data={"servings": bad}
        ).status_code == 303
    seeded_session.expire_all()
    assert seeded_session.get(Recipe, recipe.id).last_servings == 3


def test_planning_from_the_recipe_toasts_the_days(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _saved(seeded_session, web_user.id, "Plan Toast", ["1 pound spaghetti"])
    days = [date.today() + timedelta(days=1), date.today() + timedelta(days=2)]

    planned = signed_in.post(
        "/plan/add",
        data={
            "recipe_id": str(recipe.id),
            "servings": "4",
            "plan_date": [day.isoformat() for day in days],
            "next": f"/recipes/{recipe.id}",
        },
    )

    assert planned.status_code == 303
    assert planned.headers["location"] == (
        f"/recipes/{recipe.id}?planned={days[0].isoformat()},{days[1].isoformat()}"
    )
    labels = ", ".join(day.strftime("%a %-d") for day in days)
    assert f"Planned for {labels}" in signed_in.get(planned.headers["location"]).text

    plain = signed_in.post(
        "/plan/add",
        data={
            "recipe_id": str(recipe.id),
            "servings": "4",
            "plan_date": (date.today() + timedelta(days=3)).isoformat(),
        },
    )
    assert plain.headers["location"].startswith("/plan?added=")

    again = signed_in.post(
        "/plan/add",
        data={
            "recipe_id": str(recipe.id),
            "servings": "4",
            "plan_date": days[0].isoformat(),
            "next": f"/recipes/{recipe.id}/",
        },
    )
    assert again.headers["location"] == f"/recipes/{recipe.id}"


def test_naming_a_line_from_the_sheet_relinks_it_and_teaches_the_alias(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _saved(seeded_session, web_user.id, "Name Zzqx", ["2 Tbsp zzqx"])

    page = signed_in.get(f"/recipes/{recipe.id}").text

    assert 'id="needs-naming"' in page
    assert 'data-picker-submit="name"' in page
    assert f'action="/recipes/{recipe.id}/match"' in page
    assert f'action="/recipes/{recipe.id}/ignore"' in page

    named = signed_in.post(
        f"/recipes/{recipe.id}/match",
        data={"raw_text": "2 Tbsp zzqx", "name": "chicken thigh"},
    )
    assert named.status_code == 303
    seeded_session.expire_all()
    line = seeded_session.get(Recipe, recipe.id).ingredients[0]
    assert line.ingredient.name == "chicken thigh"
    learned = seeded_session.scalars(select(IngredientAlias)).all()
    assert any(alias.raw_text.casefold() == "zzqx" for alias in learned)
    assert not any("tbsp" in alias.raw_text.casefold() for alias in learned)


def test_ignoring_a_line_drops_the_needs_naming_badge(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _saved(seeded_session, web_user.id, "Ignore Zzqx", ["2 Tbsp zzqx"])
    assert '<span class="badge warn">Needs naming</span>' in signed_in.get(
        f"/recipes/{recipe.id}"
    ).text

    ignored = signed_in.post(
        f"/recipes/{recipe.id}/ignore", data={"raw_text": "2 Tbsp zzqx"}
    )

    assert ignored.status_code == 303
    assert ignored.headers["location"] == f"/recipes/{recipe.id}?toast=Ignored"
    page = signed_in.get(ignored.headers["location"]).text
    assert "Ignored" in page
    assert "Needs naming" not in page
    seeded_session.expire_all()
    assert seeded_session.get(Recipe, recipe.id).ingredients[0].is_ignored is True
    assert signed_in.post(
        f"/recipes/{recipe.id}/ignore", data={"raw_text": "no such line"}
    ).status_code == 404

