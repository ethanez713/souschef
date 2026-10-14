"""Web route tests.

Exercised through a real TestClient against a real database, because the value
here is in the wiring -- template rendering, form handling -- and a mocked
version of that proves nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import discard_item, get_or_create_item, physical_of, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.core.units import entry_unit, to_base
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


def test_home_needs_no_cookie(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.is_redirect is False


@pytest.mark.parametrize("path", ["/login", "/setup", "/join", "/logout"])
def test_account_pages_are_gone(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
    assert client.post(path).status_code == 404


def test_a_foreign_host_is_refused(client: TestClient) -> None:
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_a_tailscale_host_is_allowed_behind_the_proxy(client: TestClient) -> None:
    response = client.get(
        "/",
        headers={"Host": "x.tailnet.ts.net", "X-Forwarded-For": "100.64.0.2"},
    )
    assert response.status_code == 200


def test_a_cross_site_post_is_refused(client: TestClient) -> None:
    response = client.post("/", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_a_same_origin_post_is_allowed(client: TestClient) -> None:
    response = client.post(
        "/settings/theme",
        data={"theme": "dark"},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert response.status_code == 303


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
    assert "Scan receipt" not in response.text


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


def test_plan_page_offers_a_recipe_picker_per_day(
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
    assert 'name="plan_date"' in page.text
    assert "Chip Meal" in page.text
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
    assert "Always have" in page.text
    assert "olive oil" in page.text
    assert "Wrong match" not in page.text
    assert "Change match" in page.text
    assert f'action="/recipes/{recipe.id}/match"' in page.text


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
    # The posted 100 is pounds, the entry unit, and the ledger stays in grams.
    used = to_base(100, entry_unit(pasta, "us"), physical_of(pasta))
    assert "proposed 300" in event.note
    assert f"used {used:g}" in event.note


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
    assert "Always have" in page.text
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
    assert payload["share_target"]["enctype"] == "application/x-www-form-urlencoded"
    assert payload["display"] == "standalone"


def test_manifest_lists_a_maskable_icon_and_pages_link_the_touch_icon(
    client: TestClient,
) -> None:
    assert client.get("/manifest.webmanifest").json()["icons"] == [
        {
            "src": "/static/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/static/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/static/icon-maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    page = client.get("/")
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in page.text


TOP_LEVEL_PAGES = ("/", "/recipes", "/plan", "/shopping", "/pantry", "/receipts", "/settings")


def test_top_level_pages_have_distinct_titles(signed_in: TestClient) -> None:
    titles = []
    for path in TOP_LEVEL_PAGES:
        response = signed_in.get(path)
        assert response.status_code == 200, path
        match = re.search(r"<title>(.*?)</title>", response.text)
        assert match is not None, path
        titles.append(match.group(1))
    assert all(title.endswith(" · SousChef") and title != " · SousChef" for title in titles)
    assert len(set(titles)) == len(TOP_LEVEL_PAGES)


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
    headers = client.get("/").headers
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
    assert 'id="import-recipe"' in shared.text
    assert "data-open" in shared.text


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


def test_a_bunch_of_cilantro_uses_the_catalog_package(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "cilantro", "quantity": "1", "unit": "bunch"}
    )
    assert response.status_code == 303
    row = _pantry_row(seeded_session, web_user, "fresh cilantro")
    assert row is not None
    assert row.quantity == pytest.approx(30)
    assert "1 oz" in signed_in.get("/pantry").text


def test_a_bag_of_spinach_is_not_stored_as_a_sack_of_cement(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "spinach", "quantity": "1", "unit": "bag"}
    )
    assert response.status_code == 303
    row = _pantry_row(seeded_session, web_user, "spinach")
    assert row is not None
    assert row.quantity == pytest.approx(142)
    assert row.quantity < 1000


def test_pantry_add_parses_packages_and_kitchen_units(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    for name, quantity, unit in (
        ("spinach", "1", "bag"),
        ("chicken thighs", "2", "lb"),
        ("milk", "1", "gal"),
    ):
        response = signed_in.post(
            "/pantry/add", data={"name": name, "quantity": quantity, "unit": unit}
        )
        assert response.status_code == 303, name

    page = signed_in.get("/pantry").text
    assert "spinach" in page
    assert "5 oz" in page
    assert "1 bag" not in page
    assert "chicken thigh" in page
    assert "2 lb" in page
    assert "whole milk" in page
    assert "1 gal" in page
    assert _pantry_row(seeded_session, web_user, "spinach").quantity == pytest.approx(142)
    assert _pantry_row(seeded_session, web_user, "chicken thigh").quantity == pytest.approx(
        907.185, rel=0.01
    )
    assert _pantry_row(seeded_session, web_user, "whole milk").quantity == pytest.approx(
        3785.41, rel=0.01
    )


def test_an_unknown_package_asks_then_stores_the_size(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    asked = signed_in.post(
        "/pantry/add", data={"name": "gochujang", "quantity": "1", "unit": "jar"}
    )
    assert asked.status_code == 400
    assert "How much is one jar?" in asked.text
    assert 'value="gochujang"' in asked.text
    assert 'value="1"' in asked.text
    assert 'value="jar"' in asked.text
    seeded_session.expire_all()
    assert (
        seeded_session.scalar(
            select(CanonicalIngredient).where(CanonicalIngredient.name == "gochujang")
        )
        is None
    )

    saved = signed_in.post(
        "/pantry/add",
        data={
            "name": "gochujang",
            "quantity": "1",
            "unit": "jar",
            "package_qty": "454",
            "package_unit": "g",
        },
    )
    assert saved.status_code == 303
    page = signed_in.get("/pantry").text
    assert "gochujang" in page
    assert "1 lb" in page
    assert "1 jar" not in page

    seeded_session.expire_all()
    created = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "gochujang")
    )
    assert created is not None
    assert created.package_qty == pytest.approx(454)
    assert created.package_unit == "g"
    assert created.base_unit == "g"
    assert _pantry_row(seeded_session, web_user, "gochujang").quantity == pytest.approx(454)


def test_a_catalog_row_without_a_package_asks_before_storing(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    spinach = ing(seeded_session, "spinach")
    spinach.package_qty = None
    spinach.package_unit = None
    seeded_session.commit()

    response = signed_in.post(
        "/pantry/add", data={"name": "spinach", "quantity": "2", "unit": "bag"}
    )
    assert response.status_code == 400
    assert "How much is one bag?" in response.text
    assert 'value="spinach"' in response.text
    assert 'value="2"' in response.text
    assert _pantry_row(seeded_session, web_user, "spinach") is None


def test_an_unknown_pantry_unit_names_the_accepted_ones(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    response = signed_in.post(
        "/pantry/add", data={"name": "spinach", "quantity": "1", "unit": "furlong"}
    )
    assert response.status_code == 400
    assert "furlong" in response.text
    assert "fl oz" in response.text
    assert "carton" in response.text
    assert _pantry_row(seeded_session, web_user, "spinach") is None


def test_shopping_add_keeps_a_known_package_and_asks_for_an_unknown_one(
    signed_in: TestClient, seeded_session: Session
) -> None:
    added = signed_in.post(
        "/shopping/add", data={"name": "spinach", "amount": "1", "unit": "bag"}
    )
    assert added.status_code == 303
    page = signed_in.get("/shopping").text
    assert "1 × 5 oz" in page
    assert "spinach" in page

    asked = signed_in.post(
        "/shopping/add", data={"name": "gochujang", "amount": "1", "unit": "jar"}
    )
    assert asked.status_code == 400
    assert "How much is one jar?" in asked.text
    assert 'value="gochujang"' in asked.text
    seeded_session.expire_all()
    assert (
        seeded_session.scalar(
            select(ShoppingListItem).where(ShoppingListItem.label == "gochujang")
        )
        is None
    )
    assert (
        seeded_session.scalar(
            select(CanonicalIngredient).where(CanonicalIngredient.name == "gochujang")
        )
        is None
    )

    saved = signed_in.post(
        "/shopping/add",
        data={
            "name": "gochujang",
            "amount": "1",
            "unit": "jar",
            "package_qty": "454",
            "package_unit": "g",
        },
    )
    assert saved.status_code == 303
    again = signed_in.get("/shopping").text
    assert "gochujang" in again
    assert "1 × 1 lb" in again
    seeded_session.expire_all()
    created = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "gochujang")
    )
    assert created is not None
    assert created.package_qty == pytest.approx(454)
    assert created.package_unit == "g"
    assert created.base_unit == "g"


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
    assert '<span>Out</span>' in page.text
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
    # 80 is cups for a US user; eggs stay a count. The ledger stores milliliters.
    oil_ml = to_base(80, entry_unit(oil, "us"), physical_of(oil))
    assert _pantry_row(seeded_session, web_user, "olive oil").quantity == pytest.approx(oil_ml)
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
    before = signed_in.get(f"/recipes/{recipe.id}").text
    assert "Wrong match" not in before
    assert f'action="/recipes/{recipe.id}/match"' in before
    before_catalog = before.partition('<datalist id="catalog">')[0]
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
    assert signed_in.get("/settings/staples").headers["location"] == "/settings"

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

    wrong_method = signed_in.post("/")
    assert wrong_method.status_code == 405
    assert wrong_method.headers["content-type"].startswith("text/html")
    assert '"detail"' not in wrong_method.text


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


def test_a_night_off_shows_on_the_plan_and_adds_nothing_to_the_list(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    day = date.today().isoformat()
    posted = signed_in.post("/plan/day", data={"plan_date": day, "choice": "n:Eating out"})
    assert posted.headers["location"] == f"/plan#d-{day}"
    signed_in.post("/plan/day", data={"plan_date": day, "choice": "n:Eating out"})

    page = signed_in.get("/plan")
    assert page.text.count("<strong>Eating out</strong>") == 1
    seeded_session.expire_all()
    assert seeded_session.scalars(select(ShoppingListItem)).all() == []


def test_an_unlisted_night_off_title_is_ignored(signed_in: TestClient) -> None:
    day = date.today().isoformat()
    signed_in.post("/plan/day", data={"plan_date": day, "choice": "n:<b>x</b>"})
    assert "<b>x</b>" not in signed_in.get("/plan").text


def test_the_plan_lists_every_day_with_a_picker_and_plans_a_recipe_once(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Day Pick",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/day-pick",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    day = date.today().isoformat()
    page = signed_in.get("/plan")
    assert page.text.count('action="/plan/day"') == 10
    assert f'<option value="r:{recipe.id}">Day Pick' in page.text

    for _ in range(2):
        signed_in.post("/plan/day", data={"plan_date": day, "choice": f"r:{recipe.id}"})
    seeded_session.expire_all()
    assert len(seeded_session.scalars(select(MealPlanEntry)).all()) == 1
    assert len(seeded_session.scalars(select(ShoppingListItem)).all()) == 1


def test_a_recipe_that_is_not_yours_cannot_be_planned_by_day(
    signed_in: TestClient, seeded_session: Session
) -> None:
    other = User(username="someone-else", display_name="x", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    recipe = save_imported_recipe(
        seeded_session,
        other.id,
        RawRecipe(
            title="Theirs",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/theirs",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    posted = signed_in.post(
        "/plan/day", data={"plan_date": date.today().isoformat(), "choice": f"r:{recipe.id}"}
    )
    assert posted.status_code == 404


def test_a_planned_recipe_is_marked_with_its_day_in_add_a_meal(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Marked Meal",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/marked-meal",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    today = date.today()
    signed_in.post(
        "/plan/add",
        data={"recipe_id": recipe.id, "servings": 2, "plan_date": today.isoformat()},
    )

    assert f"planned {today.strftime('%a')}" in signed_in.get("/plan").text


def test_theme_defaults_to_light_and_follows_the_cookie(signed_in: TestClient) -> None:
    assert 'data-theme="light"' in signed_in.get("/settings").text

    chosen = signed_in.post("/settings/theme", data={"theme": "dark"})
    assert chosen.headers["location"] == "/settings"
    assert 'data-theme="dark"' in signed_in.get("/").text

    signed_in.post("/settings/theme", data={"theme": "auto"})
    assert 'data-theme="auto"' in signed_in.get("/settings").text


def test_an_unknown_theme_is_ignored(signed_in: TestClient) -> None:
    signed_in.post("/settings/theme", data={"theme": "neon"})
    signed_in.cookies.set("theme", "neon")
    assert 'data-theme="light"' in signed_in.get("/settings").text


def test_the_x_throws_an_item_out_and_offers_undo(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    spinach = ing(seeded_session, "spinach")
    record_event(seeded_session, web_user.id, spinach, reason=EventReason.PURCHASE, delta=200)
    seeded_session.commit()

    assert f"/pantry/{spinach.id}/discard" in signed_in.get("/pantry").text

    thrown = signed_in.post(f"/pantry/{spinach.id}/discard", data={"q": ""})
    assert thrown.status_code == 303
    assert "wasted=" in thrown.headers["location"]

    page = signed_in.get(thrown.headers["location"])
    assert "Threw out spinach" in page.text
    assert "Undo" in page.text
    seeded_session.expire_all()
    item = seeded_session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == web_user.id, PantryItem.ingredient_id == spinach.id
        )
    )
    assert item.quantity == 0

    event_id = int(thrown.headers["location"].rsplit("wasted=", 1)[1])
    undone = signed_in.post(f"/pantry/undo/{event_id}", data={"q": ""})
    assert undone.status_code == 303
    seeded_session.expire_all()
    assert item.quantity == 200


def test_throwing_out_from_home_returns_home(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken breast")
    long_ago = datetime.now(UTC) - timedelta(days=400)
    record_event(
        seeded_session, web_user.id, chicken, reason=EventReason.MANUAL, delta=500.0,
        occurred_at=long_ago,
    )
    get_or_create_item(seeded_session, web_user.id, chicken).last_confirmed_at = long_ago
    seeded_session.commit()

    thrown = signed_in.post(f"/pantry/{chicken.id}/discard", data={"next": "/"})
    assert thrown.headers["location"].startswith("/?wasted=")
    assert "Threw out chicken breast" in signed_in.get(thrown.headers["location"]).text


def test_the_x_is_not_offered_on_staples(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, web_user.id, oil, reason=EventReason.PURCHASE, delta=500)
    seeded_session.commit()

    assert f"/pantry/{oil.id}/discard" not in signed_in.get("/pantry").text


def test_an_undo_link_for_someone_elses_discard_changes_nothing(
    signed_in: TestClient, seeded_session: Session
) -> None:
    other = User(username="someone-else", display_name="x", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    eggs = ing(seeded_session, "eggs")
    record_event(seeded_session, other.id, eggs, reason=EventReason.PURCHASE, delta=12)
    waste = discard_item(seeded_session, other.id, eggs)
    seeded_session.commit()

    signed_in.post(f"/pantry/undo/{waste.id}", data={})
    seeded_session.expire_all()
    theirs = seeded_session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == other.id, PantryItem.ingredient_id == eggs.id
        )
    )
    assert theirs.quantity == 0
    notice = signed_in.get("/pantry", params={"wasted": waste.id})
    assert "Threw out" not in notice.text


def _pasta_recipe(session: Session, user: User, servings: int = 4):  # noqa: ANN202
    return save_imported_recipe(
        session,
        user.id,
        RawRecipe(
            title="Pasta Night",
            ingredients=["400 g spaghetti"],
            servings=servings,
            source_url="https://example.com/pasta-servings",
        ),
        source=RecipeSource.URL,
    )


def _needed(session: Session, user: User, ingredient: CanonicalIngredient) -> float:
    session.expire_all()
    return sum(
        item.needed_qty
        for item in session.scalars(
            select(ShoppingListItem).where(ShoppingListItem.ingredient_id == ingredient.id)
        )
    )


def test_editing_a_planned_meals_servings_rescales_the_list_and_is_remembered(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _pasta_recipe(seeded_session, web_user)
    pasta = ing(seeded_session, "spaghetti")
    day = date.today() + timedelta(days=1)
    entry = MealPlanEntry(
        user_id=web_user.id, plan_date=day, slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()
    signed_in.get("/shopping")
    assert _needed(seeded_session, web_user, pasta) == 400

    changed = signed_in.post(f"/plan/{entry.id}/servings", data={"servings": "2"})
    assert changed.status_code == 303
    assert _needed(seeded_session, web_user, pasta) == 200
    seeded_session.refresh(recipe)
    assert recipe.last_servings == 2

    other_day = day + timedelta(days=1)
    signed_in.post(
        "/plan/day", data={"plan_date": other_day.isoformat(), "choice": f"r:{recipe.id}"}
    )
    seeded_session.expire_all()
    added = seeded_session.scalar(
        select(MealPlanEntry).where(
            MealPlanEntry.recipe_id == recipe.id, MealPlanEntry.plan_date == other_day
        )
    )
    assert added.servings == 2


def test_the_plan_row_has_a_servings_field(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _pasta_recipe(seeded_session, web_user)
    entry = MealPlanEntry(
        user_id=web_user.id, plan_date=date.today() + timedelta(days=1),
        slot=MealSlot.DINNER, recipe_id=recipe.id, servings=6,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    page = signed_in.get("/plan").text
    assert f'action="/plan/{entry.id}/servings"' in page
    assert 'value="6"' in page


def test_servings_must_be_between_one_and_fifty(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _pasta_recipe(seeded_session, web_user)
    entry = MealPlanEntry(
        user_id=web_user.id, plan_date=date.today() + timedelta(days=1),
        slot=MealSlot.DINNER, recipe_id=recipe.id, servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    for bad in ("0", "-1", "51", "x"):
        response = signed_in.post(f"/plan/{entry.id}/servings", data={"servings": bad})
        assert response.status_code in (400, 422), bad
    seeded_session.refresh(entry)
    assert entry.servings == 4


def test_another_users_plan_entry_servings_cannot_be_edited(
    signed_in: TestClient, seeded_session: Session
) -> None:
    other = User(username="someone-else", display_name="x", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    recipe = _pasta_recipe(seeded_session, other)
    entry = MealPlanEntry(
        user_id=other.id, plan_date=date.today() + timedelta(days=1),
        slot=MealSlot.DINNER, recipe_id=recipe.id, servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    assert signed_in.post(f"/plan/{entry.id}/servings", data={"servings": "2"}).status_code == 404
    seeded_session.refresh(entry)
    assert entry.servings == 4


def test_cooked_closes_entry_from_its_entry_id(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _pasta_recipe(seeded_session, web_user)
    pasta = ing(seeded_session, "spaghetti")
    seeded_session.commit()
    tomorrow = date.today() + timedelta(days=1)
    signed_in.post(
        "/plan/add",
        data={"recipe_id": recipe.id, "servings": 2, "plan_date": tomorrow.isoformat()},
    )
    entry = seeded_session.scalar(
        select(MealPlanEntry).where(MealPlanEntry.recipe_id == recipe.id)
    )
    signed_in.post(
        f"/recipes/{recipe.id}/cooked",
        data={
            "servings": "2",
            "entry_id": str(entry.id),
            "cooked_on": date.today().isoformat(),
        },
    )
    seeded_session.expire_all()
    assert entry.cooked_at is not None
    assert f"/recipes/{recipe.id}/cook?entry={entry.id}" not in signed_in.get("/plan").text
    listed = seeded_session.scalars(
        select(ShoppingListItem).where(ShoppingListItem.ingredient_id == pasta.id)
    ).all()
    assert not listed


def test_cook_preview_scales_to_the_servings_asked_for(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _pasta_recipe(seeded_session, web_user)
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=1000)
    seeded_session.commit()

    assert f'name="qty_{pasta.id}"' in signed_in.get(f"/recipes/{recipe.id}/cook").text
    half = signed_in.get(f"/recipes/{recipe.id}/cook", params={"servings": 2}).text
    assert 'value="2"' in half
    assert f'name="proposed_{pasta.id}" value="200.0"' in half


def test_cooking_remembers_the_servings_on_the_recipe_and_the_entry(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _pasta_recipe(seeded_session, web_user)
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=1000)
    entry = MealPlanEntry(
        user_id=web_user.id, plan_date=date.today(), slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    signed_in.post(
        f"/recipes/{recipe.id}/cooked",
        data={"servings": "3", "entry_id": str(entry.id), "cooked_on": date.today().isoformat()},
    )
    seeded_session.expire_all()
    assert recipe.last_servings == 3
    assert entry.servings == 3
    assert entry.cooked_at is not None

    page = signed_in.get(f"/recipes/{recipe.id}/cook").text
    assert f'name="proposed_{pasta.id}" value="300.0"' in page


def test_confidence_pips_follow_certainty_thresholds() -> None:
    from souschef.web.common import templates

    macros = templates.env.get_template("macros.html").module

    def lit(certainty: float) -> int:
        return str(macros.confidence_pips(certainty)).count('class="on"')

    assert (lit(1.0), lit(0.6), lit(0.59), lit(0.01), lit(0.0)) == (3, 3, 2, 2, 1)


def _amount_text(page: str, name: str) -> str:
    start = page.index(f'class="list-row-title">{name}<')
    match = re.search(r'class="num">(.*?)</span>', page[start:start + 800])
    assert match is not None, name
    return match.group(1)


def test_uncertain_levels_mark_the_amount_and_home_says_check(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    """A level under 0.6, or stock that is probably gone, ends in ? — never those words."""
    spinach = ing(seeded_session, "spinach")
    lemon = ing(seeded_session, "lemon")
    chicken = ing(seeded_session, "chicken breast")
    record_event(
        seeded_session, web_user.id, spinach,
        reason=EventReason.MANUAL, level=130, uncertainty=70,
    )
    soon = date.today() + timedelta(days=2)
    record_event(
        seeded_session, web_user.id, lemon,
        reason=EventReason.PURCHASE, delta=1, expires_on=soon,
    )
    record_event(
        seeded_session, web_user.id, chicken,
        reason=EventReason.PURCHASE, delta=500,
        expires_on=date.today() - timedelta(days=1),
    )
    seeded_session.commit()

    page = signed_in.get("/pantry").text
    assert "probably gone" not in page.casefold()
    assert _amount_text(page, "spinach").endswith("?")
    lemon_amount = _amount_text(page, "lemon")
    assert not lemon_amount.endswith("?")
    assert _amount_text(page, "chicken breast").endswith("?")
    assert "use soon" in page
    assert f"expires {soon.strftime('%-d %b')}" in page

    home = signed_in.get("/").text
    assert "<span>Check</span>" in home
    assert "Least certain" not in home


def _macros():  # noqa: ANN202
    from souschef.web.common import templates

    return templates.env.get_template("macros.html").module


def test_macros_row_renders_link_sheet_and_plain_variants() -> None:
    macros = _macros()
    link = str(macros.row("Pasta", sub="You have everything", value="20 min", href="/recipes/1"))
    assert link.startswith('<a class="list-row" href="/recipes/1"')
    assert 'class="list-row-title">Pasta<' in link
    assert 'class="list-row-sub">You have everything<' in link
    assert 'class="list-row-value">20 min<' in link

    button = str(macros.row("Rice", sheet="rice-sheet"))
    assert button.startswith('<button class="list-row" type="button" data-sheet="rice-sheet"')
    assert "list-row-sub" not in button

    assert str(macros.row("Plain")).startswith('<div class="list-row"')


def test_macros_group_header_shows_the_count_only_when_given() -> None:
    macros = _macros()
    assert 'class="count">3<' in str(macros.group_header("Ready", 3))
    assert 'class="count"' not in str(macros.group_header("Ready"))


def test_macros_sheet_toast_and_sticky_bar() -> None:
    from souschef.web.common import templates

    env = templates.env
    page = env.from_string(
        '{% from "macros.html" import sheet, toast, sticky_bar %}'
        '{% call sheet("pick", "Pick one") %}<p>body</p>{% endcall %}'
        '{{ toast("Removed", "/undo/1") }}{{ toast("Saved") }}'
        '{% call sticky_bar() %}<button>Go</button>{% endcall %}'
    ).render()
    assert '<dialog class="sheet" id="pick"' in page
    assert 'id="pick-body"><p>body</p>' in page
    assert "data-sheet-close" in page
    assert page.count('class="toast"') == 2
    assert 'action="/undo/1"' in page
    assert page.count(">Undo<") == 1
    assert '<div class="sticky-bar"><button>Go</button></div>' in page


def test_service_worker_cache_was_bumped_with_the_css_change() -> None:
    from pathlib import Path

    worker = (Path(__file__).parent.parent / "src/souschef/static/sw.js").read_text()
    assert "souschef-shell-v6" in worker


def test_pages_follow_the_signed_in_unit_system(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Unit Systems",
            ingredients=["1 pound spaghetti", "2 tsp smoked paprika", "1 cup whole milk"],
            servings=1,
            source_url="https://example.com/unit-systems",
        ),
        source=RecipeSource.URL,
    )
    chicken = ing(seeded_session, "chicken thigh")
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, chicken, reason=EventReason.PURCHASE, delta=907)
    record_event(
        seeded_session,
        web_user.id,
        pasta,
        reason=EventReason.PURCHASE,
        delta=to_base(1, "pound", physical_of(pasta)),
    )
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today() + timedelta(days=1),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=2,
        )
    )
    seeded_session.commit()

    pantry = signed_in.get("/pantry").text
    assert '<span class="num">2 lb</span>' in pantry
    assert "Set to… (lb)" in pantry
    assert "907 g" not in pantry

    recipe_page = signed_in.get(f"/recipes/{recipe.id}").text
    assert 'need <span class="num">2 tsp</span>' in recipe_page
    assert 'need <span class="num">1 cup</span>' in recipe_page
    assert 'need <span class="num">1 lb</span>' in recipe_page
    assert 'have <span class="num">1 lb</span>' in recipe_page

    shopping = signed_in.get("/shopping").text
    assert "1 × 1 lb" in shopping
    assert "needs 1 lb" in shopping
    assert "454 g" not in shopping

    web_user.unit_system = "metric"
    seeded_session.commit()

    pantry = signed_in.get("/pantry").text
    assert '<span class="num">907 g</span>' in pantry
    assert "Set to… (g)" in pantry
    assert "2 lb" not in pantry

    recipe_page = signed_in.get(f"/recipes/{recipe.id}").text
    assert 'need <span class="num">9.9 ml</span>' in recipe_page
    assert 'need <span class="num">237 ml</span>' in recipe_page
    assert 'need <span class="num">454 g</span>' in recipe_page
    assert 'have <span class="num">454 g</span>' in recipe_page

    shopping = signed_in.get("/shopping").text
    assert "1 × 454 g" in shopping
    assert "needs 454 g" in shopping


def test_pantry_set_level_stores_base_grams_from_the_entry_unit(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    response = signed_in.post(f"/pantry/{chicken.id}/level", data={"quantity": "2"})
    assert response.status_code == 303
    stored = _pantry_row(seeded_session, web_user, "chicken thigh")
    assert stored is not None
    assert stored.quantity == pytest.approx(
        to_base(2, entry_unit(chicken, "us"), physical_of(chicken))
    )

    web_user.unit_system = "metric"
    seeded_session.commit()
    response = signed_in.post(f"/pantry/{chicken.id}/level", data={"quantity": "250"})
    assert response.status_code == 303
    stored = _pantry_row(seeded_session, web_user, "chicken thigh")
    assert stored is not None
    assert stored.quantity == pytest.approx(250)


def test_recipe_page_agrees_with_the_list_about_spoiled_stock(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Old Chicken",
            ingredients=["1 pound chicken thighs", "1 pound spaghetti"],
            servings=1,
            source_url="https://example.com/old-chicken",
        ),
        source=RecipeSource.URL,
    )
    record_event(
        seeded_session,
        web_user.id,
        ing(seeded_session, "chicken thigh"),
        reason=EventReason.PURCHASE,
        delta=907,
        occurred_at=datetime.now(UTC) - timedelta(days=60),
    )
    seeded_session.commit()

    assert "Missing 2" in signed_in.get("/recipes").text

    page = signed_in.get(f"/recipes/{recipe.id}").text
    assert "probably gone" not in page.casefold()
    assert "Wrong match" not in page
    assert "have none" in page
    assert 'class="badge good"' not in page


def _row_titles(page: str) -> list[str]:
    return re.findall(r'class="list-row-title">(.*?)</span>', page)


def test_picker_ingredient_query_lists_chicken_breast_before_unrelated_rows(
    signed_in: TestClient,
) -> None:
    page = signed_in.get("/pick/ingredient", params={"q": "chick"}).text
    titles = _row_titles(page)
    assert titles[0] == "chicken breast"
    assert "chickpeas" in titles
    assert titles.index("chicken breast") < titles.index("chickpeas")
    assert titles[-1] == "Add “chick” as new"


def test_picker_ingredient_query_lists_penne(signed_in: TestClient) -> None:
    page = signed_in.get("/pick/ingredient", params={"q": "pen"}).text
    titles = _row_titles(page)
    assert "penne" in titles
    assert titles.index("penne") < titles.index("Add “pen” as new")


def test_picker_ingredient_query_matches_aliases_and_carries_row_data(
    signed_in: TestClient, seeded_session: Session
) -> None:
    chicken = ing(seeded_session, "chicken breast")
    alias_hit = signed_in.get("/pick/ingredient", params={"q": "chicken breasts"}).text
    assert "chicken breast" in alias_hit
    assert "Add “chicken breasts” as new" not in alias_hit

    prefix = signed_in.get("/pick/ingredient", params={"q": "boneless skinless"}).text
    assert _row_titles(prefix)[0] == "chicken breast"

    exact = signed_in.get("/pick/ingredient", params={"q": "chicken breast"}).text
    assert f'data-id="{chicken.id}"' in exact
    assert 'data-name="chicken breast"' in exact
    assert f'data-base-unit="{chicken.base_unit}"' in exact
    assert 'data-count-noun="' in exact
    assert f'data-package-qty="{chicken.package_qty}"' in exact
    assert f'data-package-unit="{chicken.package_unit}"' in exact
    assert 'data-package-noun="' in exact
    assert "Add “chicken breast” as new" not in exact


def test_picker_ingredient_empty_query_lists_recents(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    from sqlalchemy import update

    penne = ing(seeded_session, "penne")
    chicken = ing(seeded_session, "chicken breast")
    chickpeas = ing(seeded_session, "chickpeas")
    record_event(seeded_session, web_user.id, chicken, reason=EventReason.PURCHASE, delta=100)
    record_event(seeded_session, web_user.id, penne, reason=EventReason.PURCHASE, delta=100)
    other = User(username="picker-other", display_name="picker-other", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    record_event(seeded_session, other.id, chickpeas, reason=EventReason.PURCHASE, delta=100)
    seeded_session.commit()
    seeded_session.execute(
        update(PantryItem)
        .where(PantryItem.user_id == web_user.id, PantryItem.ingredient_id == chicken.id)
        .values(updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    seeded_session.execute(
        update(PantryItem)
        .where(PantryItem.user_id == web_user.id, PantryItem.ingredient_id == penne.id)
        .values(updated_at=datetime(2024, 6, 1, tzinfo=UTC))
    )
    seeded_session.commit()

    page = signed_in.get("/pick/ingredient").text
    titles = _row_titles(page)
    assert titles.index("penne") < titles.index("chicken breast")
    assert "chickpeas" not in titles
    assert "Add “" not in page


def test_picker_ingredient_unknown_query_offers_only_add_new(signed_in: TestClient) -> None:
    page = signed_in.get("/pick/ingredient", params={"q": "zzzz"}).text
    assert page.count('class="list-row"') == 1
    assert "Add “zzzz” as new" in page


def test_picker_recipe_omits_archived_and_lists_recent_cooks_and_plans(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    older = Recipe(
        user_id=web_user.id,
        title="Older Soup",
        servings=2,
        last_cooked_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    fresh = Recipe(
        user_id=web_user.id,
        title="Fresh Soup",
        servings=2,
        last_cooked_at=datetime(2024, 6, 1, tzinfo=UTC),
    )
    untouched = Recipe(user_id=web_user.id, title="Untouched Soup", servings=2)
    archived = Recipe(
        user_id=web_user.id,
        title="Old Stew",
        servings=2,
        is_archived=True,
        last_cooked_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    live_stew = Recipe(
        user_id=web_user.id,
        title="Weeknight Stew",
        servings=2,
        last_cooked_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    planned = Recipe(user_id=web_user.id, title="Planned Soup", servings=2)
    seeded_session.add_all([older, fresh, untouched, archived, live_stew, planned])
    seeded_session.flush()
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today(),
            slot=MealSlot.DINNER,
            recipe_id=planned.id,
            servings=2,
        )
    )
    seeded_session.commit()

    recents = signed_in.get("/pick/recipe").text
    titles = _row_titles(recents)
    assert "Old Stew" not in titles
    assert "Untouched Soup" not in titles
    assert titles.index("Planned Soup") < titles.index("Fresh Soup")
    assert titles.index("Fresh Soup") < titles.index("Older Soup")
    assert 'class="badge good">Ready<' in recents

    found = signed_in.get("/pick/recipe", params={"q": "stew"}).text
    found_titles = _row_titles(found)
    assert found_titles == ["Weeknight Stew"]
    assert "Old Stew" not in found


def test_picker_macro_renders_the_hidden_input_and_the_sheet_search() -> None:
    from souschef.web.common import templates

    page = templates.env.from_string(
        '{% from "macros.html" import picker %}'
        "{{ picker('ingredient_id', 'Ingredient', 'ingredient', 'chicken breast') }}"
    ).render()
    assert '<input type="hidden" name="ingredient_id" value="chicken breast">' in page
    assert 'hx-get="/pick/ingredient"' in page
    assert "data-picker-query" in page
    assert "chicken breast" in page

    recipe = templates.env.from_string(
        '{% from "macros.html" import picker %}'
        "{{ picker('recipe_id', 'Recipe', 'recipe', '') }}"
    ).render()
    assert 'name="recipe_id"' in recipe
    assert 'hx-get="/pick/recipe"' in recipe
    assert 'data-picker-query' in recipe


def test_picker_focuses_the_search_field_inside_the_tap_handler() -> None:
    from pathlib import Path

    source = (
        Path(__file__).parent.parent / "src" / "souschef" / "static" / "picker.js"
    ).read_text()
    opener = source.split("function openPicker", 1)[1].split("function choose", 1)[0]
    assert opener.index(".focus(") < opener.index("htmx.trigger")
