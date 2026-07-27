"""Multi-user isolation and the invite flow.

This file exists for one reason: in a multi-user app the expensive bug is not a
crash, it is a query that quietly forgets to filter by owner and shows one
person another person's kitchen. Every route that takes an id is therefore
probed here from an account that does not own it.

The assertions are deliberately about *what the second user can reach*, not
about internal call signatures -- a refactor that changes how `user_id` is
threaded through should not be able to make these pass while the isolation is
actually broken.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.conftest import WEB_PASSWORD, WEB_USERNAME

from souschef.core.inventory import get_or_create_item, pantry_view, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    Invite,
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
from souschef.web import auth

OTHER_PASSWORD = "another-long-test-password"


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None
    return found


@pytest.fixture
def stranger(seeded_session: Session) -> User:
    """A second account whose data the signed-in user must never reach."""
    user = auth.create_user(seeded_session, "stranger", OTHER_PASSWORD)
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


# ---------------------------------------------------------------- core scoping


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
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, user.id, oil, reason=EventReason.PURCHASE, delta=500)
    record_event(seeded_session, other_user.id, oil, reason=EventReason.PURCHASE, delta=500)

    from souschef.core.inventory import consume_for_recipe

    consume_for_recipe(seeded_session, user.id, {oil.id: 100}, recipe_id=1)

    assert get_or_create_item(seeded_session, user.id, oil).quantity == 400
    assert get_or_create_item(seeded_session, other_user.id, oil).quantity == 500


# ---------------------------------------------------------------- route probing


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


# ---------------------------------------------------------------- invites


def test_registration_is_closed_without_an_invite(signed_in: TestClient) -> None:
    signed_in.cookies.clear()
    page = signed_in.get("/join")
    assert "needs a valid invite" in page.text
    response = signed_in.post(
        "/join",
        data={
            "code": "made-up-code",
            "username": "gatecrasher",
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert "invalid, already used, or expired" in response.text


def test_invite_lets_exactly_one_person_join(
    signed_in: TestClient, seeded_session: Session
) -> None:
    created = signed_in.post("/settings/invites", data={"note": "for a friend"})
    assert created.status_code == 303
    code = created.headers["location"].split("new_invite=")[1]

    signed_in.cookies.clear()
    joined = signed_in.post(
        "/join",
        data={
            "code": code,
            "username": "friend",
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert joined.status_code == 303
    assert signed_in.get("/").status_code == 200  # signed in as the new account

    # The same code must not work a second time.
    signed_in.cookies.clear()
    reused = signed_in.post(
        "/join",
        data={
            "code": code,
            "username": "gatecrasher",
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert "invalid, already used, or expired" in reused.text

    seeded_session.expire_all()
    assert seeded_session.scalar(
        select(User).where(User.username == "gatecrasher")
    ) is None


def test_a_new_account_starts_with_an_empty_kitchen(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    # Not olive oil: it appears as a placeholder in the "add an item" form, so
    # asserting on its absence would pass for the wrong reason.
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "heavy cream"),
        reason=EventReason.PURCHASE, delta=500,
    )
    seeded_session.commit()
    assert "heavy cream" in signed_in.get("/pantry").text

    created = signed_in.post("/settings/invites", data={"note": ""})
    code = created.headers["location"].split("new_invite=")[1]

    signed_in.cookies.clear()
    signed_in.post(
        "/join",
        data={
            "code": code,
            "username": "newcomer",
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert "heavy cream" not in signed_in.get("/pantry").text


def test_expired_invites_are_refused(seeded_session: Session, user: User) -> None:
    invite, code = auth.create_invite(seeded_session, user)
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    seeded_session.flush()

    assert auth.find_usable_invite(seeded_session, code) is None
    with pytest.raises(ValueError, match="invalid, already used, or expired"):
        auth.redeem_invite(seeded_session, code, "toolate", OTHER_PASSWORD)


def test_invite_codes_are_not_stored_in_the_clear(
    seeded_session: Session, user: User
) -> None:
    """A database leak must not hand out working invites."""
    _, code = auth.create_invite(seeded_session, user)
    stored = seeded_session.scalars(select(Invite)).all()
    assert len(stored) == 1
    assert code not in stored[0].code_hash


def test_only_admins_can_create_invites(
    signed_in: TestClient, seeded_session: Session
) -> None:
    created = signed_in.post("/settings/invites", data={"note": ""})
    code = created.headers["location"].split("new_invite=")[1]

    signed_in.cookies.clear()
    signed_in.post(
        "/join",
        data={
            "code": code,
            "username": "plainuser",
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    # Now signed in as the non-admin account that just joined.
    assert signed_in.post("/settings/invites", data={"note": ""}).status_code == 403
    assert "Create invite" not in signed_in.get("/settings").text


def test_usernames_are_case_insensitive(seeded_session: Session) -> None:
    auth.create_user(seeded_session, "Ethan", WEB_PASSWORD)
    with pytest.raises(ValueError, match="already taken"):
        auth.create_user(seeded_session, "ETHAN", WEB_PASSWORD)


# ---------------------------------------------------------------- sessions


def test_changing_your_password_signs_out_other_devices(
    signed_in: TestClient, seeded_session: Session
) -> None:
    """A stolen cookie must not outlive the password change meant to kill it."""
    from souschef.web.app import app

    other_device = TestClient(app, follow_redirects=False)
    other_device.cookies.update(signed_in.cookies)
    assert other_device.get("/").status_code == 200

    changed = signed_in.post(
        "/settings/password",
        data={
            "current": WEB_PASSWORD,
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert changed.status_code == 303

    assert other_device.get("/").status_code == 303  # old cookie is dead
    assert signed_in.get("/").status_code == 200  # the device that changed it stays in


def test_a_session_for_a_deleted_account_stops_working(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    assert signed_in.get("/").status_code == 200

    seeded_session.delete(web_user)
    seeded_session.commit()

    assert signed_in.get("/").status_code == 303


def test_a_forged_session_cookie_is_rejected(signed_in: TestClient) -> None:
    signed_in.cookies.set(auth.SESSION_COOKIE, "not-a-real-signed-value")
    assert signed_in.get("/").status_code == 303


def test_lockout_is_per_account_not_per_box(
    signed_in: TestClient, seeded_session: Session
) -> None:
    """One person fat-fingering their password must not lock everyone else out."""
    auth.create_user(seeded_session, "housemate", OTHER_PASSWORD)
    seeded_session.commit()
    signed_in.cookies.clear()

    for _ in range(auth.MAX_ATTEMPTS):
        signed_in.post("/login", data={"username": WEB_USERNAME, "password": "wrong-guess-x"})

    assert "Too many attempts" in signed_in.post(
        "/login", data={"username": WEB_USERNAME, "password": WEB_PASSWORD}
    ).text
    assert signed_in.post(
        "/login", data={"username": "housemate", "password": OTHER_PASSWORD}
    ).status_code == 303
