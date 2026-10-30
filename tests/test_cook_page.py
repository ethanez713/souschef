"""The cook screen debits the pantry in one tap, and Undo puts it back."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import pantry_view, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    Cook,
    EventReason,
    InventoryEvent,
    MealPlanEntry,
    MealSlot,
    Recipe,
    RecipeSource,
)


def _levels(session: Session, user_id: int) -> dict[int, tuple[float, float, float]]:
    session.expire_all()
    return {
        view.ingredient.id: (view.quantity, view.low, view.high)
        for view in pantry_view(session, user_id)
    }


def test_cook_screen_is_one_done_tap(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Cook Screen",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/cook-screen",
        ),
        source=RecipeSource.URL,
    )
    entry = MealPlanEntry(
        user_id=web_user.id,
        plan_date=date.today(),
        slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=2,
    )
    seeded_session.add(entry)
    seeded_session.commit()
    pasta = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "spaghetti")
    )
    assert pasta is not None

    page = signed_in.get(f"/recipes/{recipe.id}/cook?entry={entry.id}").text

    assert page.count(">Done — update pantry</button>") == 1
    assert "About to cook" not in page
    assert "Subtract from pantry" not in page
    assert "I cooked this" not in page
    assert "Adjust amounts" in page
    assert f'name="qty_{pasta.id}"' in page
    assert f'name="unit_{pasta.id}"' in page
    assert f'name="entry_id" value="{entry.id}"' in page


def test_undo_restores_the_pantry_and_a_second_undo_is_a_no_op(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Undo Cook",
            ingredients=["1 pound spaghetti"],
            servings=1,
            source_url="https://example.com/undo-cook",
        ),
        source=RecipeSource.URL,
    )
    entry = MealPlanEntry(
        user_id=web_user.id,
        plan_date=date.today(),
        slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=1,
    )
    seeded_session.add(entry)
    pasta = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "spaghetti")
    )
    assert pasta is not None
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=1000)
    seeded_session.commit()
    before = _levels(seeded_session, web_user.id)

    cooked = signed_in.post(
        f"/recipes/{recipe.id}/cooked",
        data={"servings": "1", "entry_id": str(entry.id), "cooked_on": date.today().isoformat()},
    )

    assert cooked.status_code == 303
    assert cooked.headers["location"] == f"/recipes/{recipe.id}?cooked=1"
    seeded_session.expire_all()
    cook = seeded_session.scalar(select(Cook).where(Cook.recipe_id == recipe.id))
    assert cook is not None
    events = seeded_session.scalars(
        select(InventoryEvent).where(InventoryEvent.cook_id == cook.id)
    ).all()
    assert len(events) == 1
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 1
    assert seeded_session.get(MealPlanEntry, entry.id).cooked_at is not None
    debited = _levels(seeded_session, web_user.id)
    assert debited[pasta.id][0] < before[pasta.id][0] - 100
    page = signed_in.get(cooked.headers["location"]).text
    assert "Pantry updated" in page
    assert "Cooked today" in page
    undo_action = f'action="/recipes/{recipe.id}/cooks/{cook.id}/undo"'
    assert page.count(undo_action) == 2

    undone = signed_in.post(f"/recipes/{recipe.id}/cooks/{cook.id}/undo")

    assert undone.status_code == 303
    assert undone.headers["location"] == f"/recipes/{recipe.id}"
    seeded_session.expire_all()
    restored = _levels(seeded_session, web_user.id)
    assert restored.keys() == before.keys()
    for ingredient_id, triple in before.items():
        assert restored[ingredient_id] == pytest.approx(triple, rel=1e-4, abs=1e-3)
    assert seeded_session.get(MealPlanEntry, entry.id).cooked_at is None
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 0
    assert "Cooked today" not in signed_in.get(f"/recipes/{recipe.id}").text
    marked = seeded_session.get(Cook, cook.id)
    assert marked is not None and marked.undone_at is not None
    undone_at = marked.undone_at

    again = signed_in.post(f"/recipes/{recipe.id}/cooks/{cook.id}/undo")

    assert again.status_code == 303
    seeded_session.expire_all()
    assert seeded_session.get(Cook, cook.id).undone_at == undone_at
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 0
    assert seeded_session.get(MealPlanEntry, entry.id).cooked_at is None
    still = _levels(seeded_session, web_user.id)
    for ingredient_id, triple in restored.items():
        assert still[ingredient_id] == pytest.approx(triple, rel=1e-4, abs=1e-3)
