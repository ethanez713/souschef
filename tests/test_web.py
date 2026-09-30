"""Web route tests.

Exercised through a real TestClient against a real database, because the value
here is in the wiring -- template rendering, auth gating, form handling -- and a
mocked version of that proves nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.conftest import WEB_PASSWORD, WEB_USERNAME

from souschef.core.inventory import get_or_create_item, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    IngredientAlias,
    InventoryEvent,
    MealPlanEntry,
    MealSlot,
    PantryItem,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
    Recipe,
    RecipeSource,
    ShoppingListItem,
    StoredCredential,
    User,
)


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None
    return found


def test_first_visit_offers_setup(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Claim this SousChef" in response.text


def test_protected_routes_redirect_when_signed_out(client: TestClient) -> None:
    for path in ("/", "/pantry", "/recipes", "/plan", "/shopping", "/audit", "/settings"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].split("?", 1)[0] == "/login"


def test_setup_then_access(client: TestClient) -> None:
    response = client.post(
        "/setup",
        data={"username": WEB_USERNAME, "password": WEB_PASSWORD, "confirm": WEB_PASSWORD},
    )
    assert response.status_code == 303
    assert client.get("/").status_code == 200


def test_setup_rejects_mismatched_passwords(client: TestClient) -> None:
    response = client.post(
        "/setup",
        data={"username": WEB_USERNAME, "password": WEB_PASSWORD, "confirm": "different"},
    )
    assert "do not match" in response.text


def test_setup_rejects_short_passwords(client: TestClient) -> None:
    response = client.post(
        "/setup", data={"username": WEB_USERNAME, "password": "short", "confirm": "short"}
    )
    assert "at least 12" in response.text


def test_setup_rejects_a_malformed_username(client: TestClient) -> None:
    response = client.post(
        "/setup", data={"username": "a b", "password": WEB_PASSWORD, "confirm": WEB_PASSWORD},
    )
    assert "Username must be" in response.text


def test_setup_only_works_once(signed_in: TestClient) -> None:
    """The claim page must close behind the first account, not stay open."""
    signed_in.cookies.clear()
    response = signed_in.post(
        "/setup",
        data={"username": "squatter", "password": WEB_PASSWORD, "confirm": WEB_PASSWORD},
    )
    assert response.status_code == 400
    assert signed_in.get("/login").status_code == 200
    assert "Claim this SousChef" not in signed_in.get("/login").text


def test_wrong_password_is_rejected(signed_in: TestClient) -> None:
    signed_in.cookies.clear()
    response = signed_in.post(
        "/login", data={"username": WEB_USERNAME, "password": "wrong-password-here"}
    )
    assert "Incorrect username or password" in response.text
    assert signed_in.get("/").status_code == 303


def test_login_succeeds_with_correct_password(signed_in: TestClient) -> None:
    signed_in.cookies.clear()
    response = signed_in.post(
        "/login", data={"username": WEB_USERNAME, "password": WEB_PASSWORD}
    )
    assert response.status_code == 303
    assert signed_in.get("/").status_code == 200


def test_unknown_username_gives_the_same_error_as_a_wrong_password(
    signed_in: TestClient,
) -> None:
    """Otherwise the login form is a free list of who has an account here."""
    signed_in.cookies.clear()
    unknown = signed_in.post(
        "/login", data={"username": "nobody-here", "password": WEB_PASSWORD}
    )
    wrong = signed_in.post(
        "/login", data={"username": WEB_USERNAME, "password": "wrong-password-here"}
    )
    assert "Incorrect username or password" in unknown.text
    assert "Incorrect username or password" in wrong.text


def test_logout_clears_the_session(signed_in: TestClient) -> None:
    assert signed_in.post("/logout").status_code == 303
    assert signed_in.get("/").status_code == 303


def test_repeated_failures_lock_out(client: TestClient) -> None:
    client.post(
        "/setup",
        data={"username": WEB_USERNAME, "password": WEB_PASSWORD, "confirm": WEB_PASSWORD},
    )
    client.cookies.clear()
    from souschef.web import auth

    auth._failures.clear()
    for _ in range(auth.MAX_ATTEMPTS):
        client.post("/login", data={"username": WEB_USERNAME, "password": "nope-nope-nope"})
    response = client.post("/login", data={"username": WEB_USERNAME, "password": WEB_PASSWORD})
    assert "Too many attempts" in response.text
    auth._failures.clear()


def test_dashboard_renders_when_empty(signed_in: TestClient) -> None:
    response = signed_in.get("/")
    assert response.status_code == 200
    assert "No recipes yet" in response.text


def test_home_lists_a_recipe_that_needs_a_shop(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Shop Pasta",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/shop-pasta",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    page = signed_in.get("/")
    assert "No recipes yet" not in page.text
    assert "Shop Pasta" in page.text


def test_pantry_page_lists_stock(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "olive oil"),
        reason=EventReason.PURCHASE, delta=500,
    )
    seeded_session.commit()

    response = signed_in.get("/pantry")
    assert response.status_code == 200
    assert "olive oil" in response.text


def test_adding_to_pantry_through_the_form(
    signed_in: TestClient, seeded_session: Session
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "olive oil", "quantity": "750", "unit": "ml"}
    )
    assert response.status_code == 303
    assert "olive oil" in signed_in.get("/pantry").text


def test_unknown_ingredient_is_added_verbatim(
    signed_in: TestClient, seeded_session: Session
) -> None:
    """An off-catalog name is taken literally, not rejected or coerced.

    The user is stating what they have; the catalog does not get a veto.
    """
    response = signed_in.post(
        "/pantry/add", data={"name": "xyzzy fnord", "quantity": "1", "unit": ""}
    )
    assert response.status_code == 303
    assert "xyzzy fnord" in signed_in.get("/pantry").text
    created = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "xyzzy fnord")
    )
    assert created is not None


def test_add_does_not_coerce_to_a_fuzzy_near_match(
    signed_in: TestClient, seeded_session: Session
) -> None:
    """Typing "vinegar" must not silently become the catalog's "white vinegar"."""
    signed_in.post("/pantry/add", data={"name": "vinegar", "quantity": "1", "unit": ""})
    assert (
        seeded_session.scalar(
            select(CanonicalIngredient).where(CanonicalIngredient.name == "vinegar")
        )
        is not None
    )


def test_pantry_search_tolerates_a_typo(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "olive oil"),
        reason=EventReason.PURCHASE, delta=500,
    )
    seeded_session.commit()

    assert "olive oil" in signed_in.get("/pantry", params={"q": "oliev"}).text


def test_pantry_htmx_request_returns_only_the_list(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    """A live-search keystroke swaps in the list, not the whole page chrome."""
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "olive oil"),
        reason=EventReason.PURCHASE, delta=500,
    )
    seeded_session.commit()

    response = signed_in.get(
        "/pantry", params={"q": "olive"}, headers={"HX-Request": "true"}
    )
    assert "olive oil" in response.text
    assert "Add something" not in response.text


def test_editing_a_level_keeps_the_active_search(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, web_user.id, oil, reason=EventReason.PURCHASE, delta=500)
    seeded_session.commit()

    response = signed_in.post(
        f"/pantry/{oil.id}/confirm", data={"q": "olive"}
    )
    assert response.headers["location"] == "/pantry?q=olive"


def test_plan_page_offers_day_chips_and_an_add_recipe_button(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Chip Meal",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/chips",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    page = signed_in.get("/plan")
    assert page.status_code == 200
    assert 'href="/recipes"' in page.text
    assert 'name="plan_date"' in page.text
    assert 'type="checkbox"' in page.text
    assert 'type="date"' not in page.text


def test_recipe_pages_render(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Test Pasta",
            ingredients=["1 pound spaghetti", "2 tablespoons olive oil"],
            instructions="Boil. Toss.",
            servings=2,
            source_url="https://example.com/pasta",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    assert "Test Pasta" in signed_in.get("/recipes").text

    detail = signed_in.get(f"/recipes/{recipe.id}")
    assert detail.status_code == 200
    assert "spaghetti" in detail.text
    assert "Boil." in detail.text


def test_staples_show_as_staples_on_a_recipe(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Oiled Pasta",
            ingredients=["1 pound spaghetti", "2 tablespoons olive oil"],
            servings=2,
            source_url="https://example.com/oiled",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    page = signed_in.get(f"/recipes/{recipe.id}")
    assert "assumed on hand" in page.text
    assert "olive oil" in page.text


def test_missing_recipe_is_404(signed_in: TestClient) -> None:
    assert signed_in.get("/recipes/99999").status_code == 404


def test_cook_preview_shows_what_will_be_subtracted(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Pasta Test",
            ingredients=["1 pound spaghetti"],
            servings=1,
            source_url="https://example.com/pasta-preview",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    page = signed_in.get(f"/recipes/{recipe.id}/cook")
    assert page.status_code == 200
    assert "About to cook" in page.text
    assert "spaghetti" in page.text
    assert "Subtract from pantry" in page.text


def test_cook_preview_logs_an_edit(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Pasta Test",
            ingredients=["1 pound spaghetti"],
            servings=1,
            source_url="https://example.com/pasta-edit",
        ),
        source=RecipeSource.URL,
    )
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=1000)
    seeded_session.commit()

    signed_in.post(
        f"/recipes/{recipe.id}/cooked",
        data={"servings": 1, f"proposed_{pasta.id}": "300", f"qty_{pasta.id}": "100"},
    )
    seeded_session.expire_all()
    event = seeded_session.scalars(
        select(InventoryEvent)
        .where(InventoryEvent.reason == EventReason.COOK)
        .order_by(InventoryEvent.id.desc())
    ).first()
    assert event is not None
    assert event.note is not None
    assert "proposed 300" in event.note
    assert "used 100" in event.note


def test_marking_cooked_debits_the_pantry(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Pasta Test",
            ingredients=["1 pound spaghetti"],
            servings=1,
            source_url="https://example.com/pasta",
        ),
        source=RecipeSource.URL,
    )
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=1000)
    seeded_session.commit()

    response = signed_in.post(f"/recipes/{recipe.id}/cooked", data={"servings": 1})
    assert response.status_code == 303

    seeded_session.expire_all()
    item = get_or_create_item(seeded_session, web_user.id, pasta)
    assert item.quantity < 1000
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 1


def test_cook_preview_omits_staples(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Oiled Pasta",
            ingredients=["1 pound spaghetti", "2 tablespoons olive oil"],
            servings=2,
            source_url="https://example.com/oiled-preview",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    page = signed_in.get(f"/recipes/{recipe.id}/cook")
    assert "spaghetti" in page.text
    assert "olive oil" not in page.text


def test_cooking_never_debits_a_staple(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Oil Test",
            ingredients=["2 tablespoons olive oil"],
            servings=1,
            source_url="https://example.com/oil",
        ),
        source=RecipeSource.URL,
    )
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, web_user.id, oil, reason=EventReason.PURCHASE, delta=500)
    seeded_session.commit()

    signed_in.post(f"/recipes/{recipe.id}/cooked", data={"servings": 1})

    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, oil).quantity == 500
    assert not seeded_session.scalars(
        select(InventoryEvent).where(InventoryEvent.reason == EventReason.COOK)
    ).all()


def test_pantry_shows_a_staple_as_assumed_stocked(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, web_user.id, oil, reason=EventReason.PURCHASE, delta=500)
    seeded_session.commit()

    page = signed_in.get("/pantry")
    assert "assumed stocked" in page.text
    assert f"/pantry/{oil.id}/confirm" not in page.text


def test_shopping_list_generates_from_the_plan(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Pasta Night",
            ingredients=["1 pound spaghetti"],
            servings=4,
            source_url="https://example.com/pn",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today() + timedelta(days=1),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=4,
        )
    )
    seeded_session.commit()

    assert signed_in.post("/shopping/generate").status_code == 303
    page = signed_in.get("/shopping")
    assert "spaghetti" in page.text
    assert "Pasta Night" in page.text  # the list says why


def test_shopping_page_rebuilds_from_the_plan_without_a_button(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Auto Shop",
            ingredients=["1 pound spaghetti"],
            servings=4,
            source_url="https://example.com/auto-shop",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today() + timedelta(days=1),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=4,
        )
    )
    seeded_session.commit()

    page = signed_in.get("/shopping")
    assert "spaghetti" in page.text
    assert "Rebuild from plan" not in page.text
    assert "Build from this" not in page.text


def test_audit_page_renders_with_nothing_planned(signed_in: TestClient) -> None:
    response = signed_in.get("/audit")
    assert response.status_code == 200
    assert "Nothing is planned" in response.text


def test_receipts_page_renders(signed_in: TestClient) -> None:
    response = signed_in.get("/receipts")
    assert response.status_code == 200
    assert "Photograph a receipt" in response.text


def test_nyt_cookie_is_stored_encrypted(
    signed_in: TestClient, seeded_session: Session
) -> None:
    """The plaintext cookie must not be recoverable from the database alone."""
    secret = "NYT-S=super-secret-session-value"
    assert signed_in.post("/settings/nyt-cookie", data={"cookie": secret}).status_code == 303

    seeded_session.expire_all()
    credential = seeded_session.scalar(select(StoredCredential))
    assert credential is not None
    assert secret.encode() not in credential.ciphertext

    from souschef.core.crypto import decrypt

    assert decrypt(credential.ciphertext) == secret


def test_settings_page_never_shows_the_cookie_back(
    signed_in: TestClient, seeded_session: Session
) -> None:
    secret = "NYT-S=super-secret-session-value"
    signed_in.post("/settings/nyt-cookie", data={"cookie": secret})
    page = signed_in.get("/settings")
    assert "super-secret-session-value" not in page.text
    assert "active" in page.text


def test_cookie_can_be_removed(signed_in: TestClient, seeded_session: Session) -> None:
    signed_in.post("/settings/nyt-cookie", data={"cookie": "NYT-S=x"})
    assert signed_in.post("/settings/nyt-cookie/delete").status_code == 303
    seeded_session.expire_all()
    assert seeded_session.scalar(select(StoredCredential)) is None


def test_import_is_blocked_while_external_calls_are_disabled(signed_in: TestClient) -> None:
    """Dev and test runs must not be able to reach a real site by accident."""
    response = signed_in.post(
        "/recipes/import", data={"url": "https://example.com/some-recipe"}
    )
    assert "external calls are disabled" in response.text.lower()


def test_manifest_declares_share_target(client: TestClient) -> None:
    payload = client.get("/manifest.webmanifest").json()
    assert payload["share_target"]["action"] == "/share"
    assert payload["display"] == "standalone"


def test_share_target_extracts_a_url_from_shared_text(signed_in: TestClient) -> None:
    response = signed_in.get(
        "/share", params={"text": "Look at this https://cooking.nytimes.com/recipes/123 yum"}
    )
    assert response.status_code == 303
    assert "cooking.nytimes.com/recipes/123" in response.headers["location"]


def test_share_target_without_a_url_falls_back(signed_in: TestClient) -> None:
    response = signed_in.get("/share", params={"text": "no link here"})
    assert response.headers["location"] == "/recipes"


def test_security_headers_are_set(client: TestClient) -> None:
    headers = client.get("/login").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_a_recipe_can_be_planned_from_its_own_page(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Plan From Detail",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/plan-from-detail",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    page = signed_in.get(f"/recipes/{recipe.id}")
    assert 'action="/plan/add"' in page.text
    assert 'name="plan_date"' in page.text


def test_planned_meals_offer_a_cook_button(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Plan Then Cook",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/plan-then-cook",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    signed_in.post(
        "/plan/add",
        data={"recipe_id": recipe.id, "servings": 2, "plan_date": date.today().isoformat()},
    )

    page = signed_in.get("/plan")
    assert f"/recipes/{recipe.id}/cook?entry=" in page.text


def test_cooking_confirms_on_the_recipe_page(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Confirm Cook",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/confirm-cook",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    response = signed_in.post(f"/recipes/{recipe.id}/cooked", data={"servings": 1})
    assert response.headers["location"] == f"/recipes/{recipe.id}?cooked=1"
    assert "Pantry updated" in signed_in.get(response.headers["location"]).text


def test_recipes_can_be_searched_and_a_shared_url_prefills_the_importer(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    for title in ("Lemon Chicken", "Bean Stew"):
        save_imported_recipe(
            seeded_session,
            web_user.id,
            RawRecipe(
                title=title,
                ingredients=["1 pound spaghetti"],
                servings=2,
                source_url=f"https://example.com/{title.replace(' ', '-')}",
            ),
            source=RecipeSource.URL,
        )
    seeded_session.commit()

    found = signed_in.get("/recipes", params={"q": "lemon"}).text
    assert "Lemon Chicken" in found
    assert "Bean Stew" not in found

    shared = signed_in.get("/recipes", params={"import_url": "https://cooking.nytimes.com/recipes/9"})
    assert 'value="https://cooking.nytimes.com/recipes/9"' in shared.text
    assert "<details class=\"card\" open>" in shared.text


def test_shopping_page_points_at_the_pre_shop_check_only_when_it_has_something(
    signed_in: TestClient,
) -> None:
    assert 'href="/audit"' not in signed_in.get("/shopping").text


def _pantry_row(session: Session, user: User, name: str) -> PantryItem | None:
    session.expire_all()
    ingredient = ing(session, name)
    return session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == user.id,
            PantryItem.ingredient_id == ingredient.id,
        )
    )


def test_a_bunch_of_cilantro_stays_on_the_form_instead_of_crashing(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "cilantro", "quantity": "1", "unit": "bunch"}
    )
    assert response.status_code == 400
    assert "one bunch weighs" in response.text
    assert "<details" in response.text and "open" in response.text
    assert _pantry_row(seeded_session, web_user, "fresh cilantro") is None


def test_a_bag_of_spinach_is_not_stored_as_a_sack_of_cement(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "spinach", "quantity": "1", "unit": "bag"}
    )
    assert response.status_code == 400
    assert "one bag weighs" in response.text
    assert _pantry_row(seeded_session, web_user, "spinach") is None


def test_a_negative_pantry_amount_is_refused(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "pasta", "quantity": "-1", "unit": "lb"}
    )
    assert response.status_code == 400
    assert "more than zero" in response.text
    seeded_session.expire_all()
    assert (
        seeded_session.scalar(
            select(CanonicalIngredient).where(CanonicalIngredient.name == "pasta")
        )
        is None
    )

    eggs = ing(seeded_session, "eggs")
    corrected = signed_in.post(f"/pantry/{eggs.id}/level", data={"quantity": "-3"})
    assert corrected.status_code == 400
    assert "zero or more" in corrected.text
    assert _pantry_row(seeded_session, web_user, "eggs") is None


def test_a_finished_item_is_shown_as_out_rather_than_in_stock(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    eggs = ing(seeded_session, "eggs")
    record_event(
        seeded_session, web_user.id, eggs, reason=EventReason.PURCHASE, delta=4
    )
    seeded_session.commit()
    assert signed_in.post(f"/pantry/{eggs.id}/level", data={"quantity": "0"}).status_code == 303

    page = signed_in.get("/pantry")
    assert "<h2>Out</h2>" in page.text
    assert "eggs" in page.text
    assert "High confidence" not in page.text
    assert _pantry_row(seeded_session, web_user, "eggs").quantity == 0


def test_a_missing_page_is_html(signed_in: TestClient) -> None:
    response = signed_in.get("/recipes/99999")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "No such recipe" in response.text
    assert "Not found" in response.text
    assert '"detail"' not in response.text


def test_a_wrong_current_password_is_shown_on_the_settings_page(
    signed_in: TestClient,
) -> None:
    response = signed_in.post(
        "/settings/password",
        data={"current": "not-the-password", "password": WEB_PASSWORD, "confirm": WEB_PASSWORD},
    )
    assert response.status_code == 400
    assert "Current password is incorrect" in response.text
    assert signed_in.get("/settings").status_code == 200


def test_an_unknown_staple_is_shown_on_the_settings_page(
    signed_in: TestClient, seeded_session: Session
) -> None:
    response = signed_in.post("/settings/staples", data={"name": "garam masala"})
    assert response.status_code == 303
    assert "garam masala" in signed_in.get("/settings").text
    seeded_session.expire_all()
    created = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "garam masala")
    )
    assert created is not None
    assert created.is_staple


def test_sign_in_returns_to_the_page_that_was_requested(signed_in: TestClient) -> None:
    signed_in.cookies.clear()
    denied = signed_in.get("/pantry")
    assert denied.headers["location"] == "/login?next=%2Fpantry"
    assert 'name="next" value="/pantry"' in signed_in.get("/login?next=%2Fpantry").text

    retry = signed_in.post(
        "/login",
        data={"username": WEB_USERNAME, "password": "nope-nope-nope-x", "next": "/pantry"},
    )
    assert 'name="next" value="/pantry"' in retry.text

    logged_in = signed_in.post(
        "/login",
        data={"username": WEB_USERNAME, "password": WEB_PASSWORD, "next": "/pantry"},
    )
    assert logged_in.headers["location"] == "/pantry"


def test_sign_in_keeps_a_recipe_shared_from_the_phone(signed_in: TestClient) -> None:
    signed_in.cookies.clear()
    shared = "https://example.com/shakshuka"
    denied = signed_in.get("/share", params={"url": shared})
    assert denied.status_code == 303
    assert denied.headers["location"].startswith("/login?next=")
    assert shared in signed_in.get(denied.headers["location"]).text

    logged_in = signed_in.post(
        "/login",
        data={
            "username": WEB_USERNAME,
            "password": WEB_PASSWORD,
            "next": f"/share?url={shared}",
        },
    )
    assert logged_in.headers["location"] == f"/share?url={shared}"
    onward = signed_in.get(logged_in.headers["location"])
    assert onward.headers["location"] == f"/recipes?import_url={shared}"


def test_sign_in_drops_an_off_site_return_path(signed_in: TestClient) -> None:
    signed_in.cookies.clear()
    absolute = signed_in.post(
        "/login",
        data={
            "username": WEB_USERNAME,
            "password": WEB_PASSWORD,
            "next": "https://evil.example/steal",
        },
    )
    assert absolute.headers["location"] == "/"

    signed_in.cookies.clear()
    schemeless = signed_in.post(
        "/login",
        data={
            "username": WEB_USERNAME,
            "password": WEB_PASSWORD,
            "next": "//evil.example",
        },
    )
    assert schemeless.headers["location"] == "/"


def test_the_same_meal_is_not_planned_twice_in_one_day(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Once",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/once",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    payload = {"recipe_id": recipe.id, "servings": 2, "plan_date": date.today().isoformat()}
    assert signed_in.post("/plan/add", data=payload).headers["location"] == "/plan"

    again = signed_in.post("/plan/add", data=payload)
    assert again.headers["location"] == "/plan?skipped=1"
    assert "wasn't added again" in signed_in.get("/plan?skipped=1").text

    seeded_session.expire_all()
    planned = seeded_session.scalars(
        select(MealPlanEntry).where(MealPlanEntry.recipe_id == recipe.id)
    ).all()
    assert len(planned) == 1
    bought = seeded_session.scalars(select(ShoppingListItem)).all()
    assert len(bought) == 1
    assert bought[0].needed_qty < 600


def test_one_audit_save_records_every_answer(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    oil = ing(seeded_session, "olive oil")
    eggs = ing(seeded_session, "eggs")
    record_event(seeded_session, web_user.id, oil, reason=EventReason.PURCHASE, delta=100)
    record_event(seeded_session, web_user.id, eggs, reason=EventReason.PURCHASE, delta=2)
    seeded_session.commit()

    response = signed_in.post(
        "/audit",
        data={f"quantity_{oil.id}": "80", f"quantity_{eggs.id}": "5"},
    )
    assert response.status_code == 303
    assert _pantry_row(seeded_session, web_user, "olive oil").quantity == 80
    assert _pantry_row(seeded_session, web_user, "eggs").quantity == 5


def test_applying_a_receipt_saves_the_line_on_the_same_form(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    eggs = ing(seeded_session, "eggs")
    receipt = Receipt(
        user_id=web_user.id,
        image_path="eggs.png",
        image_sha256="apply-form",
        status=ReceiptStatus.NEEDS_REVIEW,
    )
    seeded_session.add(receipt)
    seeded_session.flush()
    line = ReceiptLine(
        receipt_id=receipt.id,
        position=0,
        raw_text="LG EGGS",
        parsed_quantity=12,
        parsed_unit="count",
        ingredient_id=eggs.id,
    )
    seeded_session.add(line)
    seeded_session.commit()

    response = signed_in.post(
        f"/receipts/{receipt.id}/apply",
        data={
            f"ingredient_{line.id}": str(eggs.id),
            f"quantity_{line.id}": "6",
            f"unit_{line.id}": "count",
            f"confirmed_{line.id}": "on",
        },
    )
    assert response.status_code == 200
    assert "1 line added" in response.text
    seeded_session.expire_all()
    assert seeded_session.get(Receipt, receipt.id).status == ReceiptStatus.APPLIED
    assert _pantry_row(seeded_session, web_user, "eggs").quantity == 6


def test_applying_a_receipt_with_nothing_ticked_does_not_lock_it(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    eggs = ing(seeded_session, "eggs")
    receipt = Receipt(
        user_id=web_user.id,
        image_path="eggs2.png",
        image_sha256="apply-empty",
        status=ReceiptStatus.NEEDS_REVIEW,
    )
    seeded_session.add(receipt)
    seeded_session.flush()
    seeded_session.add(
        ReceiptLine(
            receipt_id=receipt.id,
            position=0,
            raw_text="LG EGGS",
            ingredient_id=eggs.id,
        )
    )
    seeded_session.commit()

    response = signed_in.post(f"/receipts/{receipt.id}/apply")
    assert response.status_code == 400
    assert "No lines were confirmed" in response.text
    seeded_session.expire_all()
    assert seeded_session.get(Receipt, receipt.id).status == ReceiptStatus.NEEDS_REVIEW
    assert _pantry_row(seeded_session, web_user, "eggs") is None


def test_uploading_an_applied_receipt_again_does_not_rescan_it(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    import io

    from PIL import Image

    from souschef.ingest.receipt_service import create_receipt

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    png = buffer.getvalue()

    receipt = create_receipt(seeded_session, web_user.id, png, "receipt.png")
    receipt.status = ReceiptStatus.APPLIED
    seeded_session.add(
        ReceiptLine(
            receipt_id=receipt.id,
            position=0,
            raw_text="EGGS",
            is_confirmed=True,
            ingredient_id=ing(seeded_session, "eggs").id,
        )
    )
    seeded_session.commit()

    response = signed_in.post(
        "/receipts/upload",
        files={"image": ("receipt.png", png, "image/png")},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/receipts/{receipt.id}?already=1"
    page = signed_in.get(response.headers["location"])
    assert "already" in page.text.lower()

    seeded_session.expire_all()
    saved = seeded_session.get(Receipt, receipt.id)
    assert saved.status == ReceiptStatus.APPLIED
    assert [line.raw_text for line in saved.lines] == ["EGGS"]
    assert saved.lines[0].is_confirmed


def test_a_wrong_recipe_match_can_be_corrected_on_the_recipe(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Mirin Dish",
            ingredients=["2 Tbsp mirin"],
            servings=2,
            source_url="https://example.com/mirin",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    line = recipe.ingredients[0]
    assert line.ingredient_id is None
    # The naming box lists the whole catalog, so "steak" is an option. It must
    # not be the match the line was given.
    before_catalog = signed_in.get(f"/recipes/{recipe.id}").text.partition(
        '<datalist id="catalog">'
    )[0]
    assert "steak" not in before_catalog

    corrected = signed_in.post(
        f"/recipes/{recipe.id}/match",
        data={"raw_text": line.raw_text, "name": "mirin"},
    )
    assert corrected.status_code == 303
    seeded_session.expire_all()
    saved = seeded_session.get(Recipe, recipe.id)
    assert saved.ingredients[0].ingredient.name == "mirin"
    learned = seeded_session.scalars(select(IngredientAlias)).all()
    assert any(alias.raw_text.casefold() == "mirin" for alias in learned)
    assert not any("tbsp" in alias.raw_text.casefold() for alias in learned)

    signed_in.post(
        "/plan/add",
        data={"recipe_id": recipe.id, "servings": 2, "plan_date": date.today().isoformat()},
    )
    assert "steak" not in signed_in.get("/shopping").text.casefold()


def test_setup_username_pattern_escapes_the_hyphen(client: TestClient) -> None:
    page = client.get("/login").text
    assert 'pattern="[A-Za-z0-9][A-Za-z0-9._\\-]{2,31}"' in page


def test_unknown_paths_and_the_wrong_method_render_html(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    missing = signed_in.get("/nope")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert "Not found" in missing.text
    assert '"detail"' not in missing.text

    asked = signed_in.get("/nope", headers={"Accept": "application/json"})
    assert asked.headers["content-type"].startswith("application/json")

    refresh = signed_in.get("/pantry/add")
    assert refresh.status_code == 303
    assert refresh.headers["location"] == "/pantry"
    assert signed_in.get("/setup").headers["location"] == "/login"
    assert signed_in.get("/settings/password").headers["location"] == "/settings"

    receipt = Receipt(
        user_id=web_user.id,
        image_path="eggs.png",
        image_sha256="apply-get",
        status=ReceiptStatus.NEEDS_REVIEW,
    )
    seeded_session.add(receipt)
    seeded_session.commit()
    applied = signed_in.get(f"/receipts/{receipt.id}/apply")
    assert applied.status_code == 303
    assert applied.headers["location"] == f"/receipts/{receipt.id}"

    logout = signed_in.get("/logout")
    assert logout.status_code == 405
    assert logout.headers["content-type"].startswith("text/html")
    assert '"detail"' not in logout.text


def test_skip_link_and_nav_come_before_the_page(signed_in: TestClient) -> None:
    text = signed_in.get("/pantry").text
    assert 'href="#main"' in text
    assert 'id="main"' in text
    assert text.index("Skip to content") < text.index('aria-label="Main"') < text.index("<main")


def test_recipe_naming_inputs_share_a_catalog(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Naming",
            ingredients=["1 pound spaghetti", "2 Tbsp zzqx mystery"],
            servings=2,
            source_url="https://example.com/naming",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    page = signed_in.get(f"/recipes/{recipe.id}").text
    assert 'id="catalog"' in page
    assert page.count('list="catalog"') >= 2

    saved = signed_in.post(
        f"/recipes/{recipe.id}/match",
        data={"raw_text": "2 Tbsp zzqx mystery", "name": "paprika"},
    )
    assert saved.status_code == 303
    assert "Saved. That line is now paprika." in signed_in.get(saved.headers["location"]).text


def test_shopping_list_offers_a_copy(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Shop Copy",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/shop-copy",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    signed_in.post(
        "/plan/add",
        data={"recipe_id": recipe.id, "servings": 2, "plan_date": date.today().isoformat()},
    )

    page = signed_in.get("/shopping").text
    assert "Copy list" in page
    assert 'id="shop-plain"' in page
    assert "@media print" in signed_in.get("/static/app.css").text


def test_pantry_search_does_not_offer_an_unrelated_hit(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "lemon"),
        reason=EventReason.PURCHASE, delta=1,
    )
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "dried lentils"),
        reason=EventReason.PURCHASE, delta=200,
    )
    seeded_session.commit()

    page = signed_in.get("/pantry", params={"q": "lentil"}).text
    assert "dried lentils" in page
    assert "lemon" not in page.casefold()


def test_recipe_search_matches_an_ingredient(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Weeknight Pasta",
            ingredients=["3 cloves garlic"],
            servings=2,
            source_url="https://example.com/weeknight-pasta",
        ),
        source=RecipeSource.URL,
    )
    save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Plain Toast",
            ingredients=["1 slice bread"],
            servings=1,
            source_url="https://example.com/plain-toast",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()

    found = signed_in.get("/recipes", params={"q": "garlic"}).text
    assert "Weeknight Pasta" in found
    assert "Plain Toast" not in found


def test_a_signed_in_user_opening_join_is_told_so(signed_in: TestClient) -> None:
    page = signed_in.get("/join")
    assert page.status_code == 200
    assert "already signed in" in page.text.casefold()
    assert "needs a valid invite" not in page.text
