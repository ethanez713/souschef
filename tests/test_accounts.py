"""Owner filters on every route that takes an id.

The expensive bug is a query that forgets to filter by owner and shows one
account another account's kitchen. Every route that takes an id is probed here
with a second account's row.

The assertions are about what the acting account can reach, not about internal
call signatures -- a refactor that changes how `user_id` is threaded through
should not be able to make these pass while the filter is actually gone.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.conftest import WEB_USERNAME

from souschef.core.inventory import get_or_create_item, pantry_view, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    Receipt,
    ReceiptStatus,
    Recipe,
    RecipeSource,
    ShoppingList,
    StoredCredential,
    User,
)


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None
    return found


@pytest.fixture
def stranger(seeded_session: Session, signed_in: TestClient) -> User:
    """A second account, inserted after the web account so requests do not act as it."""
    assert signed_in is not None
    user = User(username="stranger", display_name="stranger", password_hash="x")
    seeded_session.add(user)
    seeded_session.commit()
    return user


@pytest.fixture
def stranger_recipe(seeded_session: Session, stranger: User) -> Recipe:
    recipe = save_imported_recipe(
        seeded_session,
        stranger.id,
        RawRecipe(
            title="Strangers Secret Stew",
            ingredients=["1 pound spaghetti"],
            servings=4,
            source_url="https://example.com/secret",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    return recipe


def test_pantries_do_not_bleed_between_users(
    seeded_session: Session, user: User, other_user: User
) -> None:
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, user.id, oil, reason=EventReason.PURCHASE, delta=500)

    assert [v.ingredient.name for v in pantry_view(seeded_session, user.id)] == ["olive oil"]
    assert pantry_view(seeded_session, other_user.id) == []


def test_the_same_ingredient_can_be_stocked_by_two_users(
    seeded_session: Session, user: User, other_user: User
) -> None:
    """The old schema had one pantry row per ingredient, globally."""
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, user.id, oil, reason=EventReason.PURCHASE, delta=500)
    record_event(seeded_session, other_user.id, oil, reason=EventReason.PURCHASE, delta=250)

    assert get_or_create_item(seeded_session, user.id, oil).quantity == 500
    assert get_or_create_item(seeded_session, other_user.id, oil).quantity == 250


def test_both_users_can_import_the_same_recipe(
    seeded_session: Session, user: User, other_user: User
) -> None:
    """De-duplication is per user, so one import must not overwrite the other's."""
    raw = RawRecipe(
        title="Shared Favourite",
        ingredients=["1 pound spaghetti"],
        source_url="https://example.com/same",
    )
    mine = save_imported_recipe(seeded_session, user.id, raw, source=RecipeSource.URL)
    theirs = save_imported_recipe(seeded_session, other_user.id, raw, source=RecipeSource.URL)

    assert mine.id != theirs.id
    assert mine.user_id == user.id
    assert theirs.user_id == other_user.id


def test_a_cooked_meal_only_debits_the_cooks_pantry(
    seeded_session: Session, user: User, other_user: User
) -> None:
    rice = ing(seeded_session, "white rice")
    record_event(seeded_session, user.id, rice, reason=EventReason.PURCHASE, delta=500)
    record_event(seeded_session, other_user.id, rice, reason=EventReason.PURCHASE, delta=500)

    from souschef.core.inventory import consume_for_recipe

    consume_for_recipe(seeded_session, user.id, {rice.id: 100}, recipe_id=1)

    assert get_or_create_item(seeded_session, user.id, rice).quantity == 400
    assert get_or_create_item(seeded_session, other_user.id, rice).quantity == 500


def test_another_users_recipe_is_404_not_visible(
    signed_in: TestClient, stranger_recipe: Recipe
) -> None:
    response = signed_in.get(f"/recipes/{stranger_recipe.id}")
    assert response.status_code == 404
    assert "Strangers Secret Stew" not in response.text


def test_another_users_recipe_cannot_be_marked_cooked(
    signed_in: TestClient, seeded_session: Session, stranger_recipe: Recipe
) -> None:
    response = signed_in.post(f"/recipes/{stranger_recipe.id}/cooked", data={"servings": 1})
    assert response.status_code == 404

    seeded_session.expire_all()
    assert seeded_session.get(Recipe, stranger_recipe.id).times_cooked == 0


def test_another_users_recipe_cannot_be_added_to_your_plan(
    signed_in: TestClient, seeded_session: Session, stranger_recipe: Recipe
) -> None:
    """Otherwise the plan page would render a recipe title you cannot open."""
    response = signed_in.post(
        "/plan/add",
        data={
            "recipe_id": stranger_recipe.id,
            "plan_date": date.today().isoformat(),
            "servings": 4,
        },
    )
    assert response.status_code == 404
    assert "Strangers Secret Stew" not in signed_in.get("/plan").text


def test_recipe_list_shows_only_your_own(
    signed_in: TestClient, stranger_recipe: Recipe
) -> None:
    assert "Strangers Secret Stew" not in signed_in.get("/recipes").text


def test_another_users_plan_entry_cannot_be_deleted(
    signed_in: TestClient, seeded_session: Session, stranger: User, stranger_recipe: Recipe
) -> None:
    entry = MealPlanEntry(
        user_id=stranger.id,
        plan_date=date.today(),
        slot=MealSlot.DINNER,
        recipe_id=stranger_recipe.id,
        servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    signed_in.post(f"/plan/{entry.id}/remove")

    seeded_session.expire_all()
    assert seeded_session.get(MealPlanEntry, entry.id) is not None


def test_another_users_receipt_is_not_readable(
    signed_in: TestClient, seeded_session: Session, stranger: User
) -> None:
    receipt = Receipt(
        user_id=stranger.id,
        image_path="nope.png",
        image_sha256="deadbeef",
        status=ReceiptStatus.NEEDS_REVIEW,
        store_name="SECRETMART",
    )
    seeded_session.add(receipt)
    seeded_session.commit()

    response = signed_in.get(f"/receipts/{receipt.id}")
    assert response.status_code == 404
    assert "SECRETMART" not in response.text
    assert "SECRETMART" not in signed_in.get("/receipts").text


def test_another_users_receipt_cannot_be_applied(
    signed_in: TestClient, seeded_session: Session, stranger: User
) -> None:
    """Applying someone else's receipt would write into *their* ledger."""
    receipt = Receipt(
        user_id=stranger.id,
        image_path="nope.png",
        image_sha256="deadbeef2",
        status=ReceiptStatus.NEEDS_REVIEW,
    )
    seeded_session.add(receipt)
    seeded_session.commit()

    assert signed_in.post(f"/receipts/{receipt.id}/apply").status_code == 404

    seeded_session.expire_all()
    assert seeded_session.get(Receipt, receipt.id).status == ReceiptStatus.NEEDS_REVIEW


def test_another_users_shopping_item_cannot_be_ticked_off(
    signed_in: TestClient, seeded_session: Session, stranger: User
) -> None:
    from souschef.models import ShoppingListItem

    shopping = ShoppingList(user_id=stranger.id, name="Their shop")
    seeded_session.add(shopping)
    seeded_session.flush()
    item = ShoppingListItem(shopping_list_id=shopping.id, label="caviar", needed_qty=1)
    seeded_session.add(item)
    seeded_session.commit()

    signed_in.post(f"/shopping/item/{item.id}/toggle")

    seeded_session.expire_all()
    assert seeded_session.get(ShoppingListItem, item.id).is_checked is False


def test_shopping_page_shows_only_your_own_list(
    signed_in: TestClient, seeded_session: Session, stranger: User
) -> None:
    seeded_session.add(ShoppingList(user_id=stranger.id, name="Their secret shop"))
    seeded_session.commit()
    assert "Their secret shop" not in signed_in.get("/shopping").text


def test_pantry_page_shows_only_your_own_stock(
    signed_in: TestClient, seeded_session: Session, stranger: User
) -> None:
    record_event(
        seeded_session, stranger.id, ing(seeded_session, "heavy cream"),
        reason=EventReason.PURCHASE, delta=500,
    )
    seeded_session.commit()
    assert "heavy cream" not in signed_in.get("/pantry").text


def test_nyt_cookies_are_per_user(
    signed_in: TestClient, seeded_session: Session, stranger: User, web_user: User
) -> None:
    """One person's paid subscription must not be lent to another account."""
    signed_in.post("/settings/nyt-cookie", data={"cookie": "NYT-S=mine"})
    seeded_session.expire_all()

    from souschef.ingest.recipes.importers import get_nyt_cookie

    assert get_nyt_cookie(seeded_session, web_user.id) == "NYT-S=mine"
    assert get_nyt_cookie(seeded_session, stranger.id) is None

    stored = seeded_session.scalars(select(StoredCredential)).all()
    assert [c.user_id for c in stored] == [web_user.id]


def test_deleting_a_user_takes_their_data_with_them(
    seeded_session: Session, user: User
) -> None:
    """No orphan rows pointing at an account that no longer exists."""
    record_event(
        seeded_session, user.id, ing(seeded_session, "olive oil"),
        reason=EventReason.PURCHASE, delta=500,
    )
    seeded_session.add(ShoppingList(user_id=user.id, name="Doomed"))
    seeded_session.flush()

    seeded_session.delete(user)
    seeded_session.flush()

    from souschef.models import InventoryEvent, PantryItem

    assert seeded_session.scalars(select(PantryItem)).all() == []
    assert seeded_session.scalars(select(InventoryEvent)).all() == []
    assert seeded_session.scalars(select(ShoppingList)).all() == []


def test_unit_preferences_persist(signed_in, seeded_session) -> None:  # noqa: ANN001
    from souschef.models import User

    page = signed_in.get("/settings")
    assert "Show metric in parentheses" in page.text

    signed_in.post("/settings/units", data={"unit_system": "metric", "show_alt_units": "on"})
    seeded_session.expire_all()
    user = seeded_session.scalar(select(User).where(User.username == WEB_USERNAME))
    assert (user.unit_system, user.show_alt_units) == ("metric", True)
    assert "Show US in parentheses" in signed_in.get("/settings").text

    signed_in.post("/settings/units", data={"unit_system": "us"})
    seeded_session.expire_all()
    user = seeded_session.scalar(select(User).where(User.username == WEB_USERNAME))
    assert (user.unit_system, user.show_alt_units) == ("us", False)
