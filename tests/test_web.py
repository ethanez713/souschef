"""Web route tests.

Exercised through a real TestClient against a real database, because the value
here is in the wiring -- template rendering, form handling -- and a mocked
version of that proves nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import get_or_create_item, physical_of, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.core.units import entry_label, entry_unit, format_entry_amount, to_base
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    Cook,
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
    RecipeIngredient,
    RecipeSource,
    ShoppingListItem,
    StoredCredential,
    User,
)
from souschef.web.common import PAST_DAYS, PLAN_DAYS


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
        f"/pantry/{oil.id}/check", data={"quantity": "1", "shown": "1", "q": "olive"}
    )
    assert response.headers["location"].startswith("/pantry?q=olive&changed=")


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
    assert f"/pantry/{oil.id}/check" not in page.text


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


def _shop_item_id(html: str, label: str) -> int:
    marker = re.search(rf">\s*{re.escape(label)}\s*<", html)
    assert marker is not None, label
    window = html[max(0, marker.start() - 1200) : marker.start()]
    found = re.findall(r'action="/shopping/item/(\d+)/check"', window)
    assert found, label
    return int(found[-1])


def _plan_pasta(session: Session, user: User, title: str, url: str) -> MealPlanEntry:
    recipe = save_imported_recipe(
        session,
        user.id,
        RawRecipe(title=title, ingredients=["1 pound spaghetti"], servings=1, source_url=url),
        source=RecipeSource.URL,
    )
    entry = MealPlanEntry(
        user_id=user.id,
        plan_date=date.today(),
        slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=1,
    )
    session.add(entry)
    session.commit()
    return entry


def test_two_same_day_shopping_gets_write_nothing(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Quiet Shop", "https://example.com/quiet-shop")
    assert "spaghetti" in signed_in.get("/shopping").text

    from sqlalchemy import event

    from souschef.db import get_engine

    writes: list[str] = []

    def hear(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        text = statement.decode() if isinstance(statement, bytes) else statement
        verb = text.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(text)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", hear)
    try:
        first = signed_in.get("/shopping")
        second = signed_in.get("/shopping")
    finally:
        event.remove(engine, "before_cursor_execute", hear)

    assert first.status_code == 200
    assert second.status_code == 200
    assert writes == []


def test_check_twice_leaves_the_item_checked(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Tick Twice", "https://example.com/tick-twice")
    page = signed_in.get("/shopping")
    item_id = _shop_item_id(page.text, "spaghetti")

    assert signed_in.post(
        f"/shopping/item/{item_id}/check", data={"checked": "1"}
    ).status_code == 303
    assert signed_in.post(
        f"/shopping/item/{item_id}/check", data={"checked": "1"}
    ).status_code == 303

    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None and item.is_checked is True


def test_checking_a_stale_item_is_a_404(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    entry = _plan_pasta(seeded_session, web_user, "Stale Tick", "https://example.com/stale-tick")
    page = signed_in.get("/shopping")
    item_id = _shop_item_id(page.text, "spaghetti")
    assert signed_in.post(f"/plan/{entry.id}/remove").status_code == 303

    missing = signed_in.post(f"/shopping/item/{item_id}/check", data={"checked": "1"})

    assert missing.status_code == 404
    assert "That item is no longer on the list" in missing.text
    assert 'class="toast"' in missing.text


def test_applying_a_receipt_drops_the_covered_item_on_the_next_shop_page(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Receipt Shop", "https://example.com/receipt-shop")
    assert "spaghetti" in signed_in.get("/shopping").text

    spaghetti = ing(seeded_session, "spaghetti")
    receipt = Receipt(
        user_id=web_user.id,
        image_path="spaghetti.png",
        image_sha256="apply-covers-shop",
        status=ReceiptStatus.NEEDS_REVIEW,
    )
    seeded_session.add(receipt)
    seeded_session.flush()
    line = ReceiptLine(
        receipt_id=receipt.id,
        position=0,
        raw_text="SPAGHETTI",
        parsed_quantity=500,
        parsed_unit="g",
        ingredient_id=spaghetti.id,
    )
    seeded_session.add(line)
    seeded_session.commit()

    applied = signed_in.post(
        f"/receipts/{receipt.id}/apply",
        data={
            f"ingredient_{line.id}": str(spaghetti.id),
            f"quantity_{line.id}": "500",
            f"unit_{line.id}": "g",
            f"confirmed_{line.id}": "on",
        },
    )
    assert applied.status_code == 200
    assert "spaghetti" not in signed_in.get("/shopping").text


def test_an_audit_correction_drops_a_covered_item_on_the_next_shop_page(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Audit Shop", "https://example.com/audit-shop")
    assert "spaghetti" in signed_in.get("/shopping").text

    spaghetti = ing(seeded_session, "spaghetti")
    corrected = signed_in.post(
        f"/pantry/{spaghetti.id}/check", data={"quantity": "2", "shown": "0"}
    )
    assert corrected.status_code == 303
    assert "spaghetti" not in signed_in.get("/shopping").text


def test_marking_a_staple_drops_its_row_until_it_is_cleared(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Staple Shop", "https://example.com/staple-shop")
    assert "spaghetti" in signed_in.get("/shopping").text

    marked = signed_in.post("/settings/staples", data={"name": "spaghetti"})
    assert marked.status_code == 303
    assert "spaghetti" not in signed_in.get("/shopping").text

    spaghetti = ing(seeded_session, "spaghetti")
    cleared = signed_in.post(f"/settings/staples/{spaghetti.id}/clear")
    assert cleared.status_code == 303
    assert "spaghetti" in signed_in.get("/shopping").text


def test_correcting_a_recipe_match_puts_the_ingredient_on_the_next_shop_page(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Gochujang Dish",
            ingredients=["2 Tbsp gochujang"],
            servings=2,
            source_url="https://example.com/gochujang-shop",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    line = recipe.ingredients[0]
    assert line.ingredient_id is None
    planned = signed_in.post(
        "/plan/add",
        data={"recipe_id": recipe.id, "servings": 2, "plan_date": date.today().isoformat()},
    )
    assert planned.status_code == 303
    assert "spaghetti" not in signed_in.get("/shopping").text

    corrected = signed_in.post(
        f"/recipes/{recipe.id}/match",
        data={"raw_text": line.raw_text, "name": "spaghetti"},
    )
    assert corrected.status_code == 303
    assert "spaghetti" in signed_in.get("/shopping").text


def test_audit_page_renders_with_nothing_planned(signed_in: TestClient) -> None:
    response = signed_in.get("/audit")
    assert response.status_code == 200
    assert "Nothing to check." in response.text
    assert "Go to list" in response.text
    assert "% sure" not in response.text


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


def test_pasted_recipe_is_reviewed_then_saved_with_its_ingredients(
    signed_in: TestClient, seeded_session: Session
) -> None:
    text = (Path(__file__).parent / "fixtures" / "paste_headings.txt").read_text(encoding="utf-8")
    assert "Paste the recipe text" in signed_in.get("/recipes").text

    review = signed_in.post("/recipes/paste", data={"text": text})
    assert review.status_code == 200
    assert "Skillet Chili" in review.text
    assert "1 lb ground beef" in review.text
    assert "Save recipe" in review.text
    assert 'name="edit"' in review.text
    assert seeded_session.scalar(select(Recipe).where(Recipe.title == "Skillet Chili")) is None

    saved = signed_in.post("/recipes/paste", data={"text": text, "save": "1"})
    assert saved.status_code == 303

    seeded_session.expire_all()
    recipe = seeded_session.scalar(select(Recipe).where(Recipe.title == "Skillet Chili"))
    assert recipe is not None
    assert recipe.source == RecipeSource.MANUAL
    assert recipe.servings == 4
    assert len(recipe.ingredients) == 5
    assert recipe.ingredients[0].raw_text == "1 lb ground beef"
    assert recipe.ingredients[4].raw_text == "1 tbsp chili powder"

    detail = signed_in.get(saved.headers["location"])
    assert detail.status_code == 200
    assert "Skillet Chili" in detail.text
    assert "1 lb ground beef" in detail.text


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
    cilantro = ing(seeded_session, "fresh cilantro")
    page = signed_in.get("/pantry").text
    amount, unit = _shown_level(page, cilantro.id)
    assert amount == format_entry_amount(row.quantity, cilantro, "us")
    assert unit == entry_label(cilantro, "us")
    assert "bunch" not in _check_form(page, cilantro.id)


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
    assert "chicken thigh" in page
    assert "whole milk" in page
    assert "1 bag" not in page
    spinach = ing(seeded_session, "spinach")
    chicken = ing(seeded_session, "chicken thigh")
    milk = ing(seeded_session, "whole milk")
    spinach_row = _pantry_row(seeded_session, web_user, "spinach")
    chicken_row = _pantry_row(seeded_session, web_user, "chicken thigh")
    milk_row = _pantry_row(seeded_session, web_user, "whole milk")
    assert spinach_row is not None and chicken_row is not None and milk_row is not None
    assert spinach_row.quantity == pytest.approx(142)
    assert chicken_row.quantity == pytest.approx(907.185, rel=0.01)
    assert milk_row.quantity == pytest.approx(3785.41, rel=0.01)
    for item, row in ((spinach, spinach_row), (chicken, chicken_row), (milk, milk_row)):
        amount, unit = _shown_level(page, item.id)
        assert amount == format_entry_amount(row.quantity, item, "us")
        assert unit == entry_label(item, "us")


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
    assert "1 jar" not in page

    seeded_session.expire_all()
    created = seeded_session.scalar(
        select(CanonicalIngredient).where(CanonicalIngredient.name == "gochujang")
    )
    assert created is not None
    amount, unit = _shown_level(page, created.id)
    assert amount == format_entry_amount(454, created, "us")
    assert unit == entry_label(created, "us")
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


def _checked_unit(html: str) -> str | None:
    for value, attrs in re.findall(
        r'<input type="radio" name="unit" value="([^"]*)"([^>]*)>', html
    ):
        if "checked" in attrs:
            return value
    return None


def _unit_values(html: str) -> list[str]:
    return re.findall(r'<input type="radio" name="unit" value="([^"]*)"', html)


def _pantry_add_sheet(client: TestClient, name: str) -> str:
    response = client.post("/pantry/add", data={"name": name, "quantity": "0"})
    assert response.status_code == 400
    return response.text


def _shop_add_sheet(client: TestClient, name: str) -> str:
    response = client.post("/shopping/add", data={"name": name, "amount": "0"})
    assert response.status_code == 400
    return response.text


def test_add_sheet_preselects_can_for_canned_tomatoes_and_clove_for_garlic(
    signed_in: TestClient, seeded_session: Session
) -> None:
    pantry = signed_in.get("/pantry").text
    assert 'data-picker-submit="name"' in pantry
    assert 'hx-get="/pick/ingredient"' in pantry
    assert re.search(r'name="quantity"[^>]*value="1"', pantry)
    shop = signed_in.get("/shopping").text
    assert 'hx-get="/pick/ingredient"' in shop
    assert re.search(r'name="amount"[^>]*value="1"', shop)

    tomatoes = _pantry_add_sheet(signed_in, "canned tomatoes")
    assert _checked_unit(tomatoes) == "can"
    garlic = _pantry_add_sheet(signed_in, "garlic")
    assert _checked_unit(garlic) == "clove"
    assert "1 pack = 12 cloves" in garlic

    assert _checked_unit(_shop_add_sheet(signed_in, "canned tomatoes")) == "can"
    assert _checked_unit(_shop_add_sheet(signed_in, "garlic")) == "clove"

    # A known package size with no container word still leads with the package.
    spinach = _pantry_add_sheet(signed_in, "spinach")
    assert _unit_values(spinach) == ["pack", "g", "oz", "lb"]
    assert _checked_unit(spinach) == "pack"
    assert "1 pack = 5 oz" in spinach

    beans = ing(seeded_session, "black beans")
    beans.package_noun = "tin"
    seeded_session.commit()
    tinned = _pantry_add_sheet(signed_in, "black beans")
    assert _checked_unit(tinned) == "tin"
    assert "can" in _unit_values(tinned)


def test_two_cans_store_twice_the_package_in_base_units(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    tomatoes = ing(seeded_session, "canned diced tomatoes")
    response = signed_in.post(
        "/pantry/add",
        data={"name": "canned tomatoes", "quantity": "2", "unit": "can"},
    )
    assert response.status_code == 303
    row = _pantry_row(seeded_session, web_user, "canned diced tomatoes")
    assert row is not None
    expected = to_base(
        2 * tomatoes.package_qty, tomatoes.package_unit, physical_of(tomatoes)
    )
    assert row.quantity == pytest.approx(expected)

    spinach = ing(seeded_session, "spinach")
    packs = signed_in.post(
        "/pantry/add", data={"name": "spinach", "quantity": "2", "unit": "pack"}
    )
    assert packs.status_code == 303
    leaves = _pantry_row(seeded_session, web_user, "spinach")
    assert leaves is not None
    assert leaves.quantity == pytest.approx(
        to_base(2 * spinach.package_qty, spinach.package_unit, physical_of(spinach))
    )

    cloves = signed_in.post(
        "/pantry/add", data={"name": "garlic", "quantity": "2", "unit": "clove"}
    )
    assert cloves.status_code == 303
    garlic = _pantry_row(seeded_session, web_user, "garlic")
    assert garlic is not None
    assert garlic.quantity == pytest.approx(2)


def test_change_saves_a_new_package_size(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    saved = signed_in.post(
        "/pantry/add",
        data={
            "name": "spinach",
            "quantity": "1",
            "unit": "bag",
            "package_qty": "10",
            "package_unit": "oz",
        },
    )
    assert saved.status_code == 303
    seeded_session.expire_all()
    updated = ing(seeded_session, "spinach")
    assert updated.package_qty == pytest.approx(10)
    assert updated.package_unit == "oz"
    row = _pantry_row(seeded_session, web_user, "spinach")
    assert row is not None
    assert row.quantity == pytest.approx(to_base(10, "oz", physical_of(updated)))

    shop = signed_in.post(
        "/shopping/add",
        data={
            "name": "spinach",
            "amount": "1",
            "unit": "bag",
            "package_qty": "8",
            "package_unit": "oz",
        },
    )
    assert shop.status_code == 303
    seeded_session.expire_all()
    changed = ing(seeded_session, "spinach")
    assert changed.package_qty == pytest.approx(8)
    assert changed.package_unit == "oz"


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
    corrected = signed_in.post(
        f"/pantry/{eggs.id}/check", data={"quantity": "-3", "shown": "0"}
    )
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
    posted = signed_in.post(
        f"/pantry/{eggs.id}/check", data={"quantity": "0", "shown": "4"}
    )
    assert posted.status_code == 303

    page = signed_in.get("/pantry").text
    assert '<span>Out</span>' in page
    form = _check_form(page, eggs.id)
    assert _input_value(form, "quantity") == "0"
    assert _input_value(form, "shown") == "0"
    assert page.index("<span>Out</span>") < page.index(f'action="/pantry/{eggs.id}/check"')
    assert "High confidence" not in page
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
    first = signed_in.post("/plan/add", data=payload)
    assert first.headers["location"].startswith("/plan?added=")
    added = signed_in.get(first.headers["location"]).text
    when = date.today().strftime("%a %-d %b")
    assert f"Added Once to {when}" in added
    assert _undo_action(added).startswith("/plan/")

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

    oil_set = signed_in.post(
        f"/pantry/{oil.id}/check", data={"quantity": "80", "shown": "0"}
    )
    eggs_set = signed_in.post(
        f"/pantry/{eggs.id}/check", data={"quantity": "5", "shown": "0"}
    )
    assert oil_set.status_code == eggs_set.status_code == 303
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
            ingredients=["2 Tbsp zzqx"],
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
        data={"raw_text": line.raw_text, "name": "zzqx"},
    )
    assert corrected.status_code == 303
    seeded_session.expire_all()
    saved = seeded_session.get(Recipe, recipe.id)
    assert saved.ingredients[0].ingredient.name == "zzqx"
    learned = seeded_session.scalars(select(IngredientAlias)).all()
    assert any(alias.raw_text.casefold() == "zzqx" for alias in learned)
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


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
})
_SHOP_META = re.compile(r"Shopping · \d{1,2} \w{3}–\d{1,2} \w{3} · (\d+) to get")


class _RowLeads(HTMLParser):
    """First non-empty text node inside each shopping row, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.leads: list[str] = []
        self._depth = 0
        self._svg = 0
        self._done = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if self._depth == 0:
            if tag == "div" and "shop-row" in classes:
                self._depth = 1
                self._svg = 0
                self._done = False
                self._buf = []
            return
        if tag in _VOID_TAGS:
            return
        self._depth += 1
        if tag == "svg":
            self._svg += 1

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0 or tag in _VOID_TAGS:
            return
        if tag == "svg" and self._svg:
            self._svg -= 1
        self._depth -= 1
        if self._depth == 0:
            self.leads.append(" ".join(self._buf))

    def handle_data(self, data: str) -> None:
        if not self._depth or self._done or self._svg:
            return
        text = " ".join(data.split())
        if text:
            self._buf.append(text)
            self._done = True


def _row_leads(html: str) -> list[str]:
    parser = _RowLeads()
    parser.feed(html)
    return parser.leads


def _outside_sheets(html: str) -> str:
    return re.sub(r"<dialog\b[^>]*>.*?</dialog>", "", html, flags=re.S)


def _to_get(html: str) -> str:
    match = re.search(r'<p class="meta">(.*?)</p>', html, re.S)
    assert match is not None
    text = " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())
    found = _SHOP_META.fullmatch(text)
    assert found is not None, text
    return found.group(1)


def _basket(html: str) -> str | None:
    for block in re.findall(r"<details\b[^>]*>.*?</details>", html, flags=re.S):
        if "In basket" in block:
            return block
    return None


def test_shopping_rows_lead_with_the_name_and_ticked_rows_sit_in_the_basket(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Row Layout",
            ingredients=["1 pound spaghetti"],
            servings=1,
            source_url="https://example.com/row-layout",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today(),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=1,
        )
    )
    seeded_session.commit()
    signed_in.get("/shopping")
    assert signed_in.post("/shopping/add", data={"name": "paper towels"}).status_code == 303

    before = signed_in.get("/shopping").text
    assert _row_leads(before) == ["paper towels", "spaghetti"]
    assert _to_get(before) == "2"
    assert "for Row Layout" in before
    assert "for added by you" not in before
    assert "needs " not in _outside_sheets(before)
    assert "shop-skip" not in before
    assert ">Have it<" in before
    assert ">Edit amount<" in before
    assert ">Remove<" in before
    assert "to buy" not in before
    assert _basket(before) is None
    assert '<span>Other</span><span class="group-side"><span class="count">1</span>' in before
    assert '<span>Pasta</span><span class="group-side"><span class="count">1</span>' in before

    item_id = _shop_item_id(before, "spaghetti")
    assert signed_in.post(
        f"/shopping/item/{item_id}/check", data={"checked": "1"}
    ).status_code == 303
    after = signed_in.get("/shopping").text
    basket = _basket(after)
    assert basket is not None
    assert " open" in basket.split(">", 1)[0]
    assert _row_leads(after) == ["paper towels", "spaghetti"]
    assert _to_get(after) == "1"
    assert ">spaghetti<" in basket
    assert "paper towels" not in basket
    assert f'action="/shopping/item/{item_id}/check"' in basket
    assert 'name="checked" value="0"' in basket
    assert '<span>Pasta</span>' not in after
    assert 'hx-select="#shop-list"' in after
    assert 'id="shop-list"' in after

    seeded_session.expire_all()
    towels = seeded_session.scalar(
        select(ShoppingListItem).where(ShoppingListItem.label == "paper towels")
    )
    assert towels is not None
    towels.is_stocked = True
    seeded_session.commit()
    stocked = signed_in.get("/shopping").text
    assert _to_get(stocked) == "0"
    assert _row_leads(stocked) == ["paper towels", "spaghetti"]
    stocked_basket = _basket(stocked)
    assert stocked_basket is not None
    assert "paper towels" not in stocked_basket


def _plan_sauce(session: Session, user: User) -> None:
    recipe = save_imported_recipe(
        session,
        user.id,
        RawRecipe(
            title="Steak sauce",
            ingredients=["9 ml worcestershire sauce"],
            servings=1,
            source_url="https://example.com/have-it-sauce",
        ),
        source=RecipeSource.URL,
    )
    sauce = ing(session, "worcestershire sauce")
    line = recipe.ingredients[0]
    line.ingredient_id = sauce.id
    line.quantity = 9
    line.unit = "ml"
    line.quantity_is_vague = False
    line.is_ignored = False
    session.add(
        MealPlanEntry(
            user_id=user.id,
            plan_date=date.today(),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=1,
        )
    )
    session.commit()


def test_have_it_stocks_one_package_and_a_later_one_offers_always_have(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_sauce(seeded_session, web_user)
    sauce = ing(seeded_session, "worcestershire sauce")
    page = signed_in.get("/shopping")
    item_id = _shop_item_id(page.text, "worcestershire sauce")
    seeded_session.expire_all()
    row = seeded_session.get(ShoppingListItem, item_id)
    assert row is not None
    assert row.needed_qty == pytest.approx(9)

    sheet = re.search(rf'id="shop-item-{item_id}".*?</dialog>', page.text, re.S)
    assert sheet is not None
    assert "needs " in sheet.group(0)
    assert ">Have it<" in sheet.group(0)

    first = signed_in.post(f"/shopping/item/{item_id}/have")
    assert first.status_code == 303
    marked = signed_in.get(first.headers["location"])
    assert "Marked worcestershire sauce as on hand" in marked.text
    assert "Always have" not in marked.text
    assert f"/shopping/item/{item_id}/check" not in marked.text
    assert _row_leads(marked.text) == []

    seeded_session.expire_all()
    held = seeded_session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == web_user.id,
            PantryItem.ingredient_id == sauce.id,
        )
    )
    assert held is not None
    assert held.quantity == pytest.approx(296)
    events = seeded_session.scalars(
        select(InventoryEvent).where(
            InventoryEvent.user_id == web_user.id,
            InventoryEvent.ingredient_id == sauce.id,
        )
    ).all()
    assert len(events) == 1
    assert events[0].reason == EventReason.MANUAL
    assert events[0].note == "have it"
    assert events[0].resulting_quantity == pytest.approx(296)

    events[0].occurred_at = datetime.now(UTC) - timedelta(days=1)
    record_event(
        seeded_session,
        web_user.id,
        sauce,
        reason=EventReason.WASTE,
        delta=-held.quantity,
        note="used up",
    )
    seeded_session.commit()
    assert signed_in.post("/shopping/generate").status_code == 303
    again = signed_in.get("/shopping")
    new_id = _shop_item_id(again.text, "worcestershire sauce")
    second = signed_in.post(f"/shopping/item/{new_id}/have")
    assert second.status_code == 303
    offer = signed_in.get(second.headers["location"])
    assert "Marked worcestershire sauce as on hand" in offer.text
    assert "Always have worcestershire sauce" in offer.text
    action = re.search(r'action="(/shopping/ingredient/\d+/always)"', offer.text)
    assert action is not None

    assert signed_in.post(action.group(1)).status_code == 303
    seeded_session.expire_all()
    assert seeded_session.get(CanonicalIngredient, sauce.id).is_staple is True


def test_have_it_undo_restores_the_line_and_the_pantry_level(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_sauce(seeded_session, web_user)
    sauce = ing(seeded_session, "worcestershire sauce")
    page = signed_in.get("/shopping")
    item_id = _shop_item_id(page.text, "worcestershire sauce")
    seeded_session.expire_all()
    before = _pantry_level(seeded_session, web_user.id, sauce.id)

    posted = signed_in.post(f"/shopping/item/{item_id}/have")
    assert posted.status_code == 303
    marked = signed_in.get(posted.headers["location"])
    assert "Marked worcestershire sauce as on hand" in marked.text
    action, fields = _undo_form(marked.text)
    assert action.startswith("/pantry/undo/")
    assert fields["next"] == "/shopping"
    assert _row_leads(marked.text) == []
    seeded_session.expire_all()
    assert _pantry_level(seeded_session, web_user.id, sauce.id) > before

    undone = signed_in.post(action, data=fields)
    assert undone.status_code == 303
    assert undone.headers["location"] == "/shopping"
    restored = signed_in.get(undone.headers["location"]).text
    assert "worcestershire sauce" in _row_leads(restored)
    seeded_session.expire_all()
    assert _pantry_level(seeded_session, web_user.id, sauce.id) == pytest.approx(before)
    event = seeded_session.get(InventoryEvent, int(action.rsplit("/", 1)[-1]))
    assert event is not None and event.voided_at is not None

    again = signed_in.post(action, data=fields)
    assert again.status_code == 303
    seeded_session.expire_all()
    assert _pantry_level(seeded_session, web_user.id, sauce.id) == pytest.approx(before)
    assert "worcestershire sauce" in _row_leads(signed_in.get("/shopping").text)


def test_remove_then_undo_restores_the_shopping_row(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Undo Shop", "https://example.com/undo-shop")
    page = signed_in.get("/shopping").text
    item_id = _shop_item_id(page, "spaghetti")
    assert "shop-skip" not in page

    removed = signed_in.post(f"/shopping/item/{item_id}/skip")
    assert removed.status_code == 303
    gone = signed_in.get(removed.headers["location"])
    assert "Removed spaghetti" in gone.text
    assert f'action="/shopping/item/{item_id}/unskip"' in gone.text
    assert "spaghetti" not in _row_leads(gone.text)

    assert signed_in.post(f"/shopping/item/{item_id}/unskip").status_code == 303
    back = signed_in.get("/shopping").text
    assert _shop_item_id(back, "spaghetti") == item_id
    seeded_session.expire_all()
    restored = seeded_session.get(ShoppingListItem, item_id)
    assert restored is not None
    assert restored.is_skipped is False


def test_edited_shopping_amount_survives_a_rebuild(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Edit Amount", "https://example.com/edit-amount")
    page = signed_in.get("/shopping").text
    item_id = _shop_item_id(page, "spaghetti")

    edited = signed_in.post(
        f"/shopping/item/{item_id}/amount",
        data={"amount": "3", "unit": "lb"},
    )
    assert edited.status_code == 303
    assert "Updated spaghetti" in signed_in.get(edited.headers["location"]).text

    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    assert item.purchase_qty == pytest.approx(3)
    assert item.purchase_unit == "lb"
    assert item.amount_edited is True

    assert signed_in.post("/shopping/generate").status_code == 303
    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    assert item.purchase_qty == pytest.approx(3)
    assert item.purchase_unit == "lb"
    assert item.amount_edited is True
    assert "3 lb" in signed_in.get("/shopping").text


def test_edit_amount_undo_restores_the_previous_amount(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Undo Amount", "https://example.com/undo-amount")
    page = signed_in.get("/shopping").text
    item_id = _shop_item_id(page, "spaghetti")
    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    previous_qty = item.purchase_qty
    previous_unit = item.purchase_unit
    assert previous_qty is not None
    assert item.amount_edited is False

    edited = signed_in.post(
        f"/shopping/item/{item_id}/amount",
        data={"amount": "3", "unit": "lb"},
    )
    assert edited.status_code == 303
    shown = signed_in.get(edited.headers["location"])
    assert "Updated spaghetti" in shown.text
    action, fields = _undo_form(shown.text)
    assert action == f"/shopping/item/{item_id}/amount/undo"
    assert fields["amount_edited"] == "0"
    assert float(fields["qty"]) == pytest.approx(previous_qty)
    assert fields["unit"] == (previous_unit or "")

    undone = signed_in.post(action, data=fields)
    assert undone.status_code == 303
    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    assert item.amount_edited is False
    assert item.purchase_qty == pytest.approx(previous_qty)
    assert item.purchase_unit == previous_unit

    again = signed_in.post(action, data=fields)
    assert again.status_code == 303
    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    assert item.amount_edited is False
    assert item.purchase_qty == pytest.approx(previous_qty)
    assert item.purchase_unit == previous_unit

    first = signed_in.post(
        f"/shopping/item/{item_id}/amount", data={"amount": "3", "unit": "lb"}
    )
    assert first.status_code == 303
    second = signed_in.post(
        f"/shopping/item/{item_id}/amount", data={"amount": "5", "unit": "kg"}
    )
    assert second.status_code == 303
    stacked = signed_in.get(second.headers["location"])
    undo_action, undo_fields = _undo_form(stacked.text)
    assert undo_fields["amount_edited"] == "1"
    assert float(undo_fields["qty"]) == pytest.approx(3)
    assert undo_fields["unit"] == "lb"
    assert signed_in.post(undo_action, data=undo_fields).status_code == 303
    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    assert item.purchase_qty == pytest.approx(3)
    assert item.purchase_unit == "lb"
    assert item.amount_edited is True


def test_adding_an_item_answers_with_a_toast(signed_in: TestClient) -> None:
    added = signed_in.post("/shopping/add", data={"name": "onion"})
    assert added.status_code == 303
    page = signed_in.get(added.headers["location"]).text
    assert 'class="toast"' in page
    assert 'data-toast' in page
    assert "Added yellow onion to the list" in page


def test_stocking_more_than_three_lines_names_three_then_the_rest(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Four Things", "https://example.com/four-things")
    for name in ("onion", "spinach", "ground beef"):
        added = signed_in.post("/shopping/add", data={"name": name})
        assert added.status_code == 303
    page = signed_in.get("/shopping").text
    for label in ("spaghetti", "yellow onion", "spinach", "ground beef"):
        assert signed_in.post(
            f"/shopping/item/{_shop_item_id(page, label)}/check", data={"checked": "1"}
        ).status_code == 303

    stocked = signed_in.post("/shopping/stock")
    assert stocked.status_code == 303
    names = parse_qs(urlparse(stocked.headers["location"]).query)["added"]
    assert len(names) == 4
    followed = signed_in.get(stocked.headers["location"]).text
    toast = re.search(
        r'<div class="toast" role="status" data-toast>\s*<span>(.*?)</span>', followed
    )
    assert toast is not None
    assert toast.group(1) == f"Added {', '.join(names[:3])} and 1 more to the pantry."
    assert "notice good" not in followed


def test_stock_undo_voids_both_purchases_and_leaves_rows_ticked(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Undo Stock", "https://example.com/undo-stock")
    assert signed_in.post("/shopping/add", data={"name": "onion"}).status_code == 303
    page = signed_in.get("/shopping").text
    pasta_id = _shop_item_id(page, "spaghetti")
    onion_id = _shop_item_id(page, "yellow onion")
    assert signed_in.post(
        f"/shopping/item/{pasta_id}/check", data={"checked": "1"}
    ).status_code == 303
    assert signed_in.post(
        f"/shopping/item/{onion_id}/check", data={"checked": "1"}
    ).status_code == 303
    pasta = ing(seeded_session, "spaghetti")
    onion = ing(seeded_session, "yellow onion")
    seeded_session.expire_all()
    before = {
        pasta.id: _pantry_level(seeded_session, web_user.id, pasta.id),
        onion.id: _pantry_level(seeded_session, web_user.id, onion.id),
    }

    stocked = signed_in.post("/shopping/stock")
    assert stocked.status_code == 303
    followed = signed_in.get(stocked.headers["location"])
    assert "to the pantry." in followed.text
    action, fields = _undo_form(followed.text)
    assert action == "/shopping/stock/undo"
    event_ids = [int(part) for part in fields["stocked"].split(",")]
    assert len(event_ids) == 2
    seeded_session.expire_all()
    assert _pantry_level(seeded_session, web_user.id, pasta.id) > before[pasta.id]
    assert _pantry_level(seeded_session, web_user.id, onion.id) > before[onion.id]

    undone = signed_in.post(action, data=fields)
    assert undone.status_code == 303
    seeded_session.expire_all()
    events = seeded_session.scalars(
        select(InventoryEvent).where(InventoryEvent.id.in_(event_ids))
    ).all()
    assert len(events) == 2
    assert {event.reason for event in events} == {EventReason.PURCHASE}
    assert all(event.voided_at is not None for event in events)
    assert _pantry_level(seeded_session, web_user.id, pasta.id) == pytest.approx(before[pasta.id])
    assert _pantry_level(seeded_session, web_user.id, onion.id) == pytest.approx(before[onion.id])
    rows = [
        row
        for row in seeded_session.scalars(select(ShoppingListItem)).all()
        if row.label in {"spaghetti", "yellow onion"} and not row.is_skipped
    ]
    assert {row.label for row in rows} == {"spaghetti", "yellow onion"}
    assert all(row.is_checked and not row.is_stocked for row in rows)

    again = signed_in.post(action, data=fields)
    assert again.status_code == 303
    seeded_session.expire_all()
    events = seeded_session.scalars(
        select(InventoryEvent).where(InventoryEvent.id.in_(event_ids))
    ).all()
    assert all(event.voided_at is not None for event in events)
    rows = [
        row
        for row in seeded_session.scalars(select(ShoppingListItem)).all()
        if row.label in {"spaghetti", "yellow onion"} and not row.is_skipped
    ]
    assert all(row.is_checked and not row.is_stocked for row in rows)


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
    css = signed_in.get("/static/app.css").text
    assert "@media print" in css
    assert ".sticky-bar" in css.split("@media print", 1)[1].split("}", 1)[0]


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
    assert posted.headers["location"].startswith("/plan?added=")
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
    assert f'name="choice" value="r:{recipe.id}"' in page.text
    assert "<select" not in page.text

    for _ in range(2):
        signed_in.post("/plan/day", data={"plan_date": day, "choice": f"r:{recipe.id}"})
    seeded_session.expire_all()
    assert len(seeded_session.scalars(select(MealPlanEntry)).all()) == 1
    assert len(seeded_session.scalars(select(ShoppingListItem)).all()) == 1


def _sheet(html: str, sheet_id: str) -> str:
    match = re.search(
        rf'<dialog class="sheet" id="{re.escape(sheet_id)}"[\s\S]*?</dialog>',
        html,
    )
    assert match is not None, sheet_id
    return match.group(0)


def _undo_action(html: str) -> str:
    match = re.search(
        r'action="([^"]+)"[^>]*>\s*(?:<input[^>]*>\s*)*<button type="submit">Undo</button>', html
    )
    assert match is not None
    return unescape(match.group(1))


def _undo_form(html: str) -> tuple[str, dict[str, str]]:
    match = re.search(
        r'<form method="post" action="([^"]+)"[^>]*>'
        r'((?:\s*<input type="hidden" name="[^"]*" value="[^"]*">)*)'
        r'\s*<button type="submit">Undo</button>',
        html,
    )
    assert match is not None
    action = unescape(match.group(1))
    fields = {
        unescape(name): unescape(value)
        for name, value in re.findall(r'name="([^"]*)" value="([^"]*)"', match.group(2))
    }
    assert action == _undo_action(html)
    return action, fields


def _pantry_level(session: Session, user_id: int, ingredient_id: int) -> float:
    held = session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == user_id,
            PantryItem.ingredient_id == ingredient_id,
        )
    )
    return 0.0 if held is None else held.quantity


def _plan_recipe(session: Session, user: User, title: str, url: str) -> Recipe:
    recipe = save_imported_recipe(
        session,
        user.id,
        RawRecipe(
            title=title,
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url=url,
        ),
        source=RecipeSource.URL,
    )
    session.commit()
    return recipe


def test_an_upcoming_day_sheet_lists_meals_without_a_select(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(
        seeded_session, web_user, "Sheet Meal", "https://example.com/sheet-meal"
    )
    yesterday = date.today() - timedelta(days=1)
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=yesterday,
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=2,
        )
    )
    seeded_session.commit()

    page = signed_in.get("/plan").text
    upcoming = _sheet(page, f"day-{date.today().isoformat()}")
    assert "<select" not in upcoming
    assert upcoming.count('name="choice"') == 3
    assert 'value="n:Eating out"' in upcoming
    assert 'value="n:Leftovers"' in upcoming
    assert f'value="r:{recipe.id}"' in upcoming
    assert "Missing 1–3" in upcoming

    past = _sheet(page, f"day-{yesterday.isoformat()}")
    assert "<select" not in past
    assert 'name="choice"' not in past
    assert "Sheet Meal" in past


def test_adding_from_the_day_sheet_can_be_undone(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(
        seeded_session, web_user, "Undo Meal", "https://example.com/undo-meal"
    )
    day = date.today()
    posted = signed_in.post(
        "/plan/day", data={"plan_date": day.isoformat(), "choice": f"r:{recipe.id}"}
    )
    page = signed_in.get(posted.headers["location"]).text
    assert f"Added {recipe.title} to {day.strftime('%a %-d %b')}" in page

    seeded_session.expire_all()
    entry = seeded_session.scalar(select(MealPlanEntry))
    assert entry is not None
    assert _undo_action(page) == f"/plan/{entry.id}/remove"
    assert seeded_session.scalars(select(ShoppingListItem)).all() != []

    signed_in.post(f"/plan/{entry.id}/remove")
    seeded_session.expire_all()
    assert seeded_session.scalars(select(MealPlanEntry)).all() == []
    assert seeded_session.scalars(select(ShoppingListItem)).all() == []


def test_removing_a_meal_can_be_undone(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(
        seeded_session, web_user, "Restore Meal", "https://example.com/restore-meal"
    )
    day = date.today() + timedelta(days=1)
    entry = MealPlanEntry(
        user_id=web_user.id,
        plan_date=day,
        slot=MealSlot.LUNCH,
        recipe_id=recipe.id,
        servings=6,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    removed = signed_in.post(f"/plan/{entry.id}/remove")
    query = parse_qs(urlparse(removed.headers["location"]).query)
    assert query["recipe"] == [str(recipe.id)]
    assert query["date"] == [day.isoformat()]
    assert query["servings"] == ["6"]
    assert query["slot"] == ["lunch"]
    assert "title" not in query
    page = signed_in.get(removed.headers["location"]).text
    assert f"Removed {recipe.title}" in page

    signed_in.post(_undo_action(page))
    seeded_session.expire_all()
    restored = seeded_session.scalars(select(MealPlanEntry)).all()
    assert len(restored) == 1
    assert restored[0].recipe_id == recipe.id
    assert restored[0].plan_date == day
    assert restored[0].servings == 6
    assert restored[0].slot == MealSlot.LUNCH
    assert seeded_session.scalars(select(ShoppingListItem)).all() != []


def test_removing_a_night_off_can_be_undone(
    signed_in: TestClient, seeded_session: Session
) -> None:
    day = date.today()
    posted = signed_in.post(
        "/plan/day", data={"plan_date": day.isoformat(), "choice": "n:Eating out"}
    )
    removed = signed_in.post(_undo_action(signed_in.get(posted.headers["location"]).text))
    gone = signed_in.get(removed.headers["location"]).text
    assert "Removed Eating out" in gone
    assert "<strong>Eating out</strong>" not in gone

    signed_in.post(_undo_action(gone))
    seeded_session.expire_all()
    restored = seeded_session.scalars(select(MealPlanEntry)).all()
    assert len(restored) == 1
    assert restored[0].freeform_title == "Eating out"
    assert restored[0].plan_date == day
    assert restored[0].servings == 4
    assert restored[0].slot == MealSlot.DINNER


def test_a_freeform_undo_title_is_capped_and_escaped(
    signed_in: TestClient, seeded_session: Session
) -> None:
    raw = "<b>" + ("n" * 250)
    day = date.today()
    page = signed_in.get(
        "/plan",
        params={"title": raw, "date": day.isoformat(), "servings": "4", "slot": "dinner"},
    ).text
    assert "<b>" not in page
    assert "Removed &lt;b&gt;" in page
    assert "n" * 198 not in page
    assert "n" * 197 in page

    signed_in.post(_undo_action(page))
    seeded_session.expire_all()
    entry = seeded_session.scalar(select(MealPlanEntry))
    assert entry is not None
    assert entry.freeform_title == raw[:200]
    assert len(entry.freeform_title) == 200


def test_plan_restore_rejects_a_bad_undo(
    signed_in: TestClient, seeded_session: Session
) -> None:
    day = date.today().isoformat()
    quiet = signed_in.get(
        "/plan",
        params={"title": "Eating out", "date": day, "servings": "4", "slot": "brunch"},
    )
    assert 'class="toast"' not in quiet.text
    rejected = signed_in.post(
        "/plan/restore",
        params={"title": "Eating out", "date": day, "servings": "99", "slot": "dinner"},
    )
    assert rejected.status_code == 303
    assert rejected.headers["location"] == "/plan"
    seeded_session.expire_all()
    assert seeded_session.scalars(select(MealPlanEntry)).all() == []


def test_plan_undo_does_not_reveal_another_users_recipe(
    signed_in: TestClient, seeded_session: Session
) -> None:
    other = User(username="someone-else", display_name="x", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    recipe = save_imported_recipe(
        seeded_session,
        other.id,
        RawRecipe(
            title="Hidden Stew",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/hidden-stew",
        ),
        source=RecipeSource.URL,
    )
    entry = MealPlanEntry(
        user_id=other.id,
        plan_date=date.today(),
        slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=2,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    hidden = signed_in.get("/plan", params={"added": str(entry.id)})
    assert "Hidden Stew" not in hidden.text
    assert 'class="toast"' not in hidden.text
    listed = signed_in.get(
        "/plan",
        params={
            "recipe": str(recipe.id),
            "date": date.today().isoformat(),
            "servings": "2",
            "slot": "dinner",
        },
    )
    assert "Hidden Stew" not in listed.text
    denied = signed_in.post(
        "/plan/restore",
        params={
            "recipe": str(recipe.id),
            "date": date.today().isoformat(),
            "servings": "2",
            "slot": "dinner",
        },
    )
    assert denied.status_code == 404
    seeded_session.expire_all()
    assert seeded_session.get(MealPlanEntry, entry.id) is not None
    assert (
        seeded_session.scalars(
            select(MealPlanEntry).where(MealPlanEntry.user_id != other.id)
        ).all()
        == []
    )


def test_the_day_sheet_filters_only_after_eight_recipes(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    for number in range(8):
        save_imported_recipe(
            seeded_session,
            web_user.id,
            RawRecipe(
                title=f"Counted Meal {number}",
                ingredients=["1 pound spaghetti"],
                servings=2,
                source_url=f"https://example.com/counted-{number}",
            ),
            source=RecipeSource.URL,
        )
    seeded_session.commit()
    assert 'class="dayadd-filter"' not in signed_in.get("/plan").text

    save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Ninth Meal",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/counted-ninth",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.commit()
    page = signed_in.get("/plan").text
    assert page.count('class="dayadd-filter"') == PLAN_DAYS
    assert "autofocus" not in page


def test_moving_a_meal_to_another_day_can_be_undone(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(seeded_session, web_user, "Move Meal", "https://example.com/move-meal")
    origin = date.today()
    target = origin + timedelta(days=1)
    entry = MealPlanEntry(
        user_id=web_user.id,
        plan_date=origin,
        slot=MealSlot.LUNCH,
        recipe_id=recipe.id,
        servings=6,
    )
    note = MealPlanEntry(
        user_id=web_user.id,
        plan_date=target,
        slot=MealSlot.DINNER,
        freeform_title="Eating out",
    )
    seeded_session.add_all([entry, note])
    seeded_session.commit()
    seeded_session.refresh(entry)
    stamped = entry.updated_at

    moved = signed_in.post(f"/plan/{entry.id}/move", data={"plan_date": target.isoformat()})
    assert moved.status_code == 303
    assert moved.headers["location"] == f"/plan?moved={entry.id}&from={origin.isoformat()}"
    seeded_session.expire_all()
    seeded_session.refresh(entry)
    assert entry.plan_date == target
    assert entry.servings == 6
    assert entry.slot == MealSlot.LUNCH
    assert entry.updated_at == stamped
    assert seeded_session.scalars(select(ShoppingListItem)).all() != []

    page = signed_in.get(moved.headers["location"]).text
    when = target.strftime("%a %-d %b")
    assert f"Moved {recipe.title} to {when}" in page
    action, fields = _undo_form(page)
    assert action == f"/plan/{entry.id}/move"
    assert fields == {"plan_date": origin.isoformat()}

    undone = signed_in.post(action, data=fields)
    assert undone.status_code == 303
    seeded_session.expire_all()
    seeded_session.refresh(entry)
    assert entry.plan_date == origin
    assert entry.servings == 6
    assert entry.slot == MealSlot.LUNCH

    note_move = signed_in.post(f"/plan/{note.id}/move", data={"plan_date": origin.isoformat()})
    assert "title=" not in note_move.headers["location"]
    note_page = signed_in.get(note_move.headers["location"]).text
    assert f"Moved Eating out to {origin.strftime('%a %-d %b')}" in note_page
    seeded_session.expire_all()
    seeded_session.refresh(note)
    assert note.plan_date == origin
    assert note.freeform_title == "Eating out"


def test_moving_onto_a_day_that_already_has_that_meal_is_blocked(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(
        seeded_session, web_user, "Blocked Meal", "https://example.com/blocked-meal"
    )
    today = date.today()
    other = today + timedelta(days=2)
    first = MealPlanEntry(
        user_id=web_user.id, plan_date=today, slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2,
    )
    second = MealPlanEntry(
        user_id=web_user.id, plan_date=other, slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2,
    )
    eating = MealPlanEntry(
        user_id=web_user.id, plan_date=today, slot=MealSlot.DINNER, freeform_title="Eating out",
    )
    again = MealPlanEntry(
        user_id=web_user.id, plan_date=other, slot=MealSlot.DINNER, freeform_title="Eating out",
    )
    seeded_session.add_all([first, second, eating, again])
    seeded_session.commit()

    blocked = signed_in.post(f"/plan/{first.id}/move", data={"plan_date": other.isoformat()})
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/plan?blocked=1"
    page = signed_in.get(blocked.headers["location"]).text
    assert "That meal is already on that day, so it stayed where it was." in page
    assert 'class="toast"' not in page
    seeded_session.expire_all()
    seeded_session.refresh(first)
    assert first.plan_date == today

    note_blocked = signed_in.post(
        f"/plan/{eating.id}/move", data={"plan_date": other.isoformat()}
    )
    assert note_blocked.headers["location"] == "/plan?blocked=1"
    seeded_session.expire_all()
    seeded_session.refresh(eating)
    assert eating.plan_date == today


def test_a_cooked_or_skipped_meal_cannot_be_moved(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(seeded_session, web_user, "Closed Meal", "https://example.com/closed-meal")
    today = date.today()
    cooked = MealPlanEntry(
        user_id=web_user.id, plan_date=today, slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2, cooked_at=datetime.now(UTC),
    )
    skipped = MealPlanEntry(
        user_id=web_user.id, plan_date=today + timedelta(days=1), slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2, skipped_at=datetime.now(UTC),
    )
    seeded_session.add_all([cooked, skipped])
    seeded_session.commit()
    target = (today + timedelta(days=3)).isoformat()

    assert signed_in.post(f"/plan/{cooked.id}/move", data={"plan_date": target}).status_code == 404
    assert signed_in.post(f"/plan/{skipped.id}/move", data={"plan_date": target}).status_code == 404
    seeded_session.expire_all()
    seeded_session.refresh(cooked)
    seeded_session.refresh(skipped)
    assert cooked.plan_date == today
    assert skipped.plan_date == today + timedelta(days=1)


def test_a_move_outside_the_shown_window_leaves_the_meal(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(seeded_session, web_user, "Window Meal", "https://example.com/window-meal")
    today = date.today()
    home = today + timedelta(days=1)
    entry = MealPlanEntry(
        user_id=web_user.id, plan_date=home, slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=4,
    )
    seeded_session.add(entry)
    seeded_session.commit()
    past_start = today - timedelta(days=PAST_DAYS)
    week_end = today + timedelta(days=PLAN_DAYS - 1)

    kept = signed_in.post(f"/plan/{entry.id}/move", data={"plan_date": week_end.isoformat()})
    assert kept.headers["location"] == f"/plan?moved={entry.id}&from={home.isoformat()}"
    back = signed_in.post(f"/plan/{entry.id}/move", data={"plan_date": past_start.isoformat()})
    assert back.headers["location"] == f"/plan?moved={entry.id}&from={week_end.isoformat()}"
    seeded_session.expire_all()
    seeded_session.refresh(entry)
    assert entry.plan_date == past_start

    for day in (past_start - timedelta(days=1), week_end + timedelta(days=1), past_start):
        response = signed_in.post(
            f"/plan/{entry.id}/move", data={"plan_date": day.isoformat()}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/plan"
    seeded_session.expire_all()
    seeded_session.refresh(entry)
    assert entry.plan_date == past_start
    assert entry.servings == 4


def test_the_plan_page_offers_a_move_handle_only_for_an_open_meal(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _plan_recipe(seeded_session, web_user, "Handle Meal", "https://example.com/handle-meal")
    today = date.today()
    yesterday = today - timedelta(days=1)
    open_entry = MealPlanEntry(
        user_id=web_user.id, plan_date=today, slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2,
    )
    cooked = MealPlanEntry(
        user_id=web_user.id, plan_date=today + timedelta(days=1), slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2, cooked_at=datetime.now(UTC),
    )
    past = MealPlanEntry(
        user_id=web_user.id, plan_date=yesterday, slot=MealSlot.DINNER,
        freeform_title="Eating out",
    )
    seeded_session.add_all([open_entry, cooked, past])
    seeded_session.commit()

    page = signed_in.get("/plan").text
    assert f'data-move="{open_entry.id}"' in page
    assert f'data-move="{past.id}"' in page
    assert f'data-move="{cooked.id}"' not in page
    assert f'aria-label="Move {recipe.title}"' in page
    assert 'aria-label="Move Eating out"' in page
    assert page.count("data-drop") == PLAN_DAYS
    assert "plan.js" in page
    yesterday_row = re.search(
        rf'<div class="list-row plan-day" id="d-{yesterday.isoformat()}"[^>]*>', page
    )
    today_row = re.search(
        rf'<div class="list-row plan-day today" id="d-{today.isoformat()}"[^>]*>', page
    )
    assert yesterday_row is not None and "data-drop" not in yesterday_row.group(0)
    assert today_row is not None and "data-drop" in today_row.group(0)
    assert 'class="plan-spacer"' in page


def test_a_move_toast_ignores_a_bad_or_foreign_entry(
    signed_in: TestClient, seeded_session: Session
) -> None:
    other = User(username="someone-else", display_name="x", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    recipe = save_imported_recipe(
        seeded_session,
        other.id,
        RawRecipe(
            title="Hidden Move",
            ingredients=["1 pound spaghetti"],
            servings=2,
            source_url="https://example.com/hidden-move",
        ),
        source=RecipeSource.URL,
    )
    entry = MealPlanEntry(
        user_id=other.id, plan_date=date.today(), slot=MealSlot.DINNER,
        recipe_id=recipe.id, servings=2,
    )
    seeded_session.add(entry)
    seeded_session.commit()

    hidden = signed_in.get(
        "/plan", params={"moved": str(entry.id), "from": date.today().isoformat()}
    )
    assert "Hidden Move" not in hidden.text
    assert 'class="toast"' not in hidden.text
    garbled = signed_in.get("/plan", params={"moved": "nope", "from": "yesterday"})
    assert 'class="toast"' not in garbled.text
    denied = signed_in.post(
        f"/plan/{entry.id}/move",
        data={"plan_date": (date.today() + timedelta(days=1)).isoformat()},
    )
    assert denied.status_code == 404
    seeded_session.expire_all()
    assert seeded_session.get(MealPlanEntry, entry.id).plan_date == date.today()


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


def _ledger(session: Session, user: User, ingredient: CanonicalIngredient) -> list[InventoryEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(InventoryEvent).where(
                InventoryEvent.user_id == user.id,
                InventoryEvent.ingredient_id == ingredient.id,
            )
        )
    )


def test_an_unchanged_check_records_one_confirm(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    garlic = ing(seeded_session, "garlic")
    record_event(seeded_session, web_user.id, garlic, reason=EventReason.PURCHASE, delta=3)
    seeded_session.commit()

    page = signed_in.get("/pantry").text
    form = _check_form(page, garlic.id)
    shown = _input_value(form, "shown")
    assert shown == _input_value(form, "quantity")
    assert 'inputmode="decimal"' in form
    assert "/discard" not in form
    assert "edit-toggle" not in page

    posted = signed_in.post(
        f"/pantry/{garlic.id}/check",
        data={"quantity": shown, "shown": shown},
        headers={"HX-Request": "true"},
    )
    assert posted.status_code == 303
    assert posted.headers["location"].startswith("/pantry?changed=")
    fragment = signed_in.get(posted.headers["location"], headers={"HX-Request": "true"})
    assert "Scan receipt" not in fragment.text
    assert "Confirmed garlic" in fragment.text
    assert ">Undo<" in fragment.text

    events = _ledger(seeded_session, web_user, garlic)
    assert [event.reason for event in events].count(EventReason.CONFIRM) == 1
    assert EventReason.MANUAL not in [event.reason for event in events]
    assert _pantry_row(seeded_session, web_user, "garlic").quantity == 3


def test_an_edited_check_records_one_manual_at_that_level(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    garlic = ing(seeded_session, "garlic")
    record_event(seeded_session, web_user.id, garlic, reason=EventReason.PURCHASE, delta=3)
    seeded_session.commit()

    posted = signed_in.post(
        f"/pantry/{garlic.id}/check", data={"quantity": "5", "shown": "3"}
    )
    assert posted.status_code == 303
    page = signed_in.get(posted.headers["location"]).text
    assert "Set garlic to 5 cloves" in page

    events = _ledger(seeded_session, web_user, garlic)
    manuals = [event for event in events if event.reason == EventReason.MANUAL]
    assert len(manuals) == 1
    assert manuals[0].resulting_quantity == 5
    assert EventReason.CONFIRM not in [event.reason for event in events]
    assert _pantry_row(seeded_session, web_user, "garlic").quantity == 5


def test_setting_zero_moves_the_row_to_out(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    garlic = ing(seeded_session, "garlic")
    record_event(seeded_session, web_user.id, garlic, reason=EventReason.PURCHASE, delta=3)
    seeded_session.commit()

    posted = signed_in.post(
        f"/pantry/{garlic.id}/check", data={"quantity": "0", "shown": "3"}
    )
    assert posted.status_code == 303
    page = signed_in.get("/pantry").text
    assert '<span>Out</span>' in page
    assert page.index("<span>Out</span>") < page.index(f'action="/pantry/{garlic.id}/check"')
    form = _check_form(page, garlic.id)
    assert _input_value(form, "quantity") == "0"
    events = _ledger(seeded_session, web_user, garlic)
    assert EventReason.WASTE not in [event.reason for event in events]
    assert _pantry_row(seeded_session, web_user, "garlic").quantity == 0


def test_undo_voids_the_edit_restores_the_level_and_rebuilds_the_list(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan_pasta(seeded_session, web_user, "Undo Shop", "https://example.com/undo-shop")
    assert "spaghetti" in signed_in.get("/shopping").text

    spaghetti = ing(seeded_session, "spaghetti")
    posted = signed_in.post(
        f"/pantry/{spaghetti.id}/check", data={"quantity": "2", "shown": "0"}
    )
    assert "spaghetti" not in signed_in.get("/shopping").text
    event_id = int(posted.headers["location"].rsplit("changed=", 1)[1])
    stored = _pantry_row(seeded_session, web_user, "spaghetti")
    assert stored.quantity == pytest.approx(
        to_base(2, entry_unit(spaghetti, "us"), physical_of(spaghetti))
    )

    undone = signed_in.post(f"/pantry/undo/{event_id}", data={"q": ""})
    assert undone.status_code == 303
    seeded_session.expire_all()
    assert stored.quantity == 0
    event = seeded_session.get(InventoryEvent, event_id)
    assert event is not None and event.voided_at is not None
    assert "spaghetti" in signed_in.get("/shopping").text
    stale = signed_in.get("/pantry", params={"changed": event_id})
    assert 'class="toast"' not in stale.text


def test_home_check_renders_the_quantity_row(
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

    home = signed_in.get("/").text
    assert "<span>Check</span>" in home
    form = _check_form(home, chicken.id)
    assert 'name="next" value="/"' in form
    assert 'name="quantity"' in form
    assert 'inputmode="decimal"' in form
    assert "edit-toggle" not in home
    assert f"/pantry/{chicken.id}/discard" not in home

    shown = _input_value(form, "shown")
    posted = signed_in.post(
        f"/pantry/{chicken.id}/check",
        data={"quantity": shown, "shown": shown, "next": "/"},
    )
    assert posted.headers["location"].startswith("/?changed=")
    assert "Confirmed chicken breast" in signed_in.get(posted.headers["location"]).text
    escaped = signed_in.post(
        f"/pantry/{chicken.id}/check",
        data={"quantity": shown, "shown": shown, "next": "https://example.com/x"},
    )
    assert escaped.headers["location"].startswith("/?changed=")
    assert "example.com" not in escaped.headers["location"]


def test_adding_answers_with_an_undoable_toast(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    added = signed_in.post(
        "/pantry/add", data={"name": "black beans", "quantity": "1", "unit": "can"}
    )
    assert added.status_code == 303
    assert "changed=" in added.headers["location"]
    page = signed_in.get(added.headers["location"]).text
    assert "Added 1 can black beans" in page
    event_id = int(added.headers["location"].rsplit("changed=", 1)[1])
    assert _pantry_row(seeded_session, web_user, "black beans").quantity == 1

    undone = signed_in.post(f"/pantry/undo/{event_id}")
    assert undone.status_code == 303
    seeded_session.expire_all()
    assert _pantry_row(seeded_session, web_user, "black beans").quantity == 0


def test_the_audit_page_drops_a_checked_row(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    rice = ing(seeded_session, "white rice")
    record_event(seeded_session, web_user.id, rice, reason=EventReason.PURCHASE, delta=500)
    item = seeded_session.scalar(
        select(PantryItem).where(PantryItem.ingredient_id == rice.id)
    )
    assert item is not None
    item.belief_at = datetime.now(UTC) - timedelta(days=90)
    save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Rice Bowl",
            ingredients=["480 g white rice"],
            servings=1,
            source_url="https://example.com/rice-audit",
        ),
        source=RecipeSource.URL,
    )
    recipe = seeded_session.scalar(select(Recipe).where(Recipe.title == "Rice Bowl"))
    assert recipe is not None
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today(),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=1,
        )
    )
    seeded_session.commit()

    page = signed_in.get("/audit").text
    assert "% sure" not in page
    assert "physical look" not in page
    assert "Check these 1 before you shop." in page
    form = _check_form(page, rice.id)
    assert 'name="next" value="/audit"' in form
    assert "need " in form
    shown = _input_value(form, "shown")
    posted = signed_in.post(
        f"/pantry/{rice.id}/check",
        data={"quantity": shown, "shown": shown, "next": "/audit"},
    )
    assert posted.headers["location"].startswith("/audit?changed=")
    after = signed_in.get(posted.headers["location"]).text
    assert f'action="/pantry/{rice.id}/check"' not in after
    assert "Nothing to check." in after
    assert "Confirmed white rice" in after
    assert "Go to list" in after
    assert "% sure" not in after


def test_an_undo_of_someone_elses_event_changes_nothing(
    signed_in: TestClient, seeded_session: Session
) -> None:
    other = User(username="someone-else", display_name="x", password_hash="x")
    seeded_session.add(other)
    seeded_session.flush()
    eggs = ing(seeded_session, "eggs")
    bought = record_event(seeded_session, other.id, eggs, reason=EventReason.PURCHASE, delta=12)
    seeded_session.commit()

    signed_in.post(f"/pantry/undo/{bought.id}", data={})
    seeded_session.expire_all()
    theirs = seeded_session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == other.id, PantryItem.ingredient_id == eggs.id
        )
    )
    assert theirs.quantity == 12
    notice = signed_in.get("/pantry", params={"changed": bought.id})
    assert 'class="toast"' not in notice.text
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


def test_confidence_mark_follows_certainty_thresholds() -> None:
    from souschef.web.common import templates

    macros = templates.env.get_template("macros.html").module

    def tier(certainty: float, gone: bool = False) -> str:
        html = str(macros.confidence_mark(certainty, gone))
        if 'class="conf low"' in html:
            return "low?"
        if 'class="conf med"' in html:
            return "med~"
        return "" if "conf" not in html else "unknown"

    assert tier(1.0) == tier(0.6) == ""
    assert tier(0.59) == tier(0.01) == "med~"
    assert tier(0.0) == tier(1.0, gone=True) == "low?"


def _check_form(page: str, ingredient_id: int) -> str:
    marker = f'action="/pantry/{ingredient_id}/check"'
    start = page.index(marker)
    return page[start:page.index("</form>", start)]


def _input_value(form: str, name: str) -> str:
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', form)
    assert match is not None, name
    return match.group(1)


def _shown_level(page: str, ingredient_id: int) -> tuple[str, str]:
    form = _check_form(page, ingredient_id)
    unit = re.search(r'class="qty-unit"[^>]*>([^<]*)<', form)
    assert unit is not None
    return _input_value(form, "quantity"), unit.group(1)


def _mark(page: str, name: str) -> str | None:
    """The bubble on this row only. The next title starts the next row."""
    start = page.index(f'class="list-row-title">{name}')
    ends = [
        i for i in (
            page.find('class="list-row-sub"', start),
            page.find('class="list-row-trail"', start),
        ) if i != -1
    ]
    match = re.search(r'class="conf (low|med)"', page[start:min(ends)])
    return match.group(1) if match else None


def test_uncertain_levels_get_a_confidence_bubble_and_home_says_check(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    """A level under 0.6, or stock that is probably gone, gets a bubble — never those words."""
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
    for item in (spinach, lemon, chicken):
        assert not _input_value(_check_form(page, item.id), "quantity").endswith("?")
    assert _mark(page, "spinach") is not None
    assert _mark(page, "lemon") is None
    assert _mark(page, "chicken breast") == "low"
    sub_lines = "".join(re.findall(r'class="list-row-sub">(.*?)</span>', page))
    assert 'class="conf' not in sub_lines
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
    assert "souschef-shell-v8" in worker


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
    chicken_form = _check_form(pantry, chicken.id)
    assert _input_value(chicken_form, "quantity") == "2"
    assert ">lb<" in chicken_form
    assert "Set to…" not in pantry
    assert "907 g" not in pantry

    recipe_page = signed_in.get(f"/recipes/{recipe.id}").text
    assert 'need <span class="num">2 tsp</span>' in recipe_page
    assert 'need <span class="num">1 cup</span>' in recipe_page
    assert 'need <span class="num">1 lb</span>' in recipe_page
    assert 'have <span class="num">1 lb</span>' in recipe_page

    shopping = signed_in.get("/shopping").text
    assert "1 × 1 lb" in shopping
    assert "needs 1 lb" in shopping
    assert "needs 1 lb" not in _outside_sheets(shopping)
    assert "454 g" not in shopping

    web_user.unit_system = "metric"
    seeded_session.commit()

    pantry = signed_in.get("/pantry").text
    chicken_form = _check_form(pantry, chicken.id)
    assert _input_value(chicken_form, "quantity") == "905"
    assert ">g<" in chicken_form
    assert "2 lb" not in pantry

    recipe_page = signed_in.get(f"/recipes/{recipe.id}").text
    assert 'need <span class="num">9.9 ml</span>' in recipe_page
    assert 'need <span class="num">237 ml</span>' in recipe_page
    assert 'need <span class="num">454 g</span>' in recipe_page
    assert 'have <span class="num">454 g</span>' in recipe_page

    shopping = signed_in.get("/shopping").text
    assert "1 × 454 g" in shopping
    assert "needs 454 g" in shopping
    assert "needs 454 g" not in _outside_sheets(shopping)


def test_pantry_set_level_stores_base_grams_from_the_entry_unit(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    response = signed_in.post(
        f"/pantry/{chicken.id}/check", data={"quantity": "2", "shown": "0"}
    )
    assert response.status_code == 303
    stored = _pantry_row(seeded_session, web_user, "chicken thigh")
    assert stored is not None
    assert stored.quantity == pytest.approx(
        to_base(2, entry_unit(chicken, "us"), physical_of(chicken))
    )

    web_user.unit_system = "metric"
    seeded_session.commit()
    response = signed_in.post(
        f"/pantry/{chicken.id}/check", data={"quantity": "250", "shown": "0"}
    )
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


def test_picker_ranks_a_later_word_match_above_fuzzy_hits(signed_in: TestClient) -> None:
    rows = re.findall(r'data-name="([^"]*)"', signed_in.get("/pick/ingredient?q=paste").text)
    assert {"tomato paste", "miso paste"} <= set(rows[:3])


def test_the_form_creates_a_recipe(signed_in: TestClient, seeded_session: Session) -> None:
    blank = signed_in.post(
        "/recipes",
        data={"title": "  ", "servings": "2", "ingredients": "1 pound spaghetti"},
    )
    assert blank.status_code == 400
    assert "Enter a title." in blank.text

    page = signed_in.get("/recipes").text
    assert 'href="/recipes/new"' in page
    assert "Add a recipe" in page
    assert "Paste recipe" in page
    form = signed_in.get("/recipes/new")
    assert form.status_code == 200
    assert "Add a recipe" in form.text
    for name in ("title", "servings", "minutes", "source_url", "ingredients", "method"):
        assert f'name="{name}"' in form.text

    created = signed_in.post(
        "/recipes",
        data={
            "title": "Weeknight noodles",
            "servings": "2",
            "minutes": "25",
            "source_url": "https://example.com/noodles",
            "ingredients": "1 pound spaghetti\n2 tablespoons olive oil",
            "method": "Boil the water.\nToss.",
        },
    )
    assert created.status_code == 303
    seeded_session.expire_all()
    recipe = seeded_session.scalar(select(Recipe).where(Recipe.title == "Weeknight noodles"))
    assert recipe is not None
    assert recipe.source == RecipeSource.MANUAL
    assert recipe.external_id is None
    assert recipe.servings == 2
    assert recipe.total_minutes == 25
    assert recipe.source_url == "https://example.com/noodles"
    assert recipe.instructions == "Boil the water.\n\nToss."
    assert [line.raw_text for line in recipe.ingredients] == [
        "1 pound spaghetti",
        "2 tablespoons olive oil",
    ]
    assert all(line.ingredient_id is not None for line in recipe.ingredients)

    detail = signed_in.get(created.headers["location"])
    assert detail.status_code == 200
    assert "Weeknight noodles" in detail.text
    assert 'class="toast"' in detail.text
    assert f'href="/recipes/{recipe.id}/edit"' in detail.text


def test_editing_one_line_rematches_only_that_line(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    onion = ing(seeded_session, "yellow onion")
    spaghetti = ing(seeded_session, "spaghetti")
    recipe = Recipe(
        user_id=web_user.id,
        title="Onion stew",
        servings=4,
        source=RecipeSource.MANUAL,
        instructions="Simmer.",
    )
    seeded_session.add(recipe)
    seeded_session.flush()
    first = RecipeIngredient(
        recipe_id=recipe.id,
        position=0,
        raw_text="2 onions",
        ingredient_id=spaghetti.id,
        match_confidence=0.42,
        quantity=2,
        unit="count",
    )
    second = RecipeIngredient(
        recipe_id=recipe.id,
        position=1,
        raw_text="2 onions",
        ingredient_id=onion.id,
        match_confidence=0.17,
        quantity=2,
        unit="count",
    )
    seeded_session.add_all([
        first,
        second,
        RecipeIngredient(
            recipe_id=recipe.id,
            position=2,
            raw_text="1 carrot",
            match_confidence=0.05,
        ),
    ])
    seeded_session.commit()
    first_id, second_id = first.id, second.id

    saved = signed_in.post(
        f"/recipes/{recipe.id}",
        data={
            "title": "Onion stew",
            "servings": "4",
            "minutes": "",
            "ingredients": "2 onions\n2 onions\n1 lb chicken breast",
            "method": "Simmer.",
        },
    )
    assert saved.status_code == 303

    seeded_session.expire_all()
    lines = list(seeded_session.get(Recipe, recipe.id).ingredients)
    assert [line.raw_text for line in lines] == [
        "2 onions",
        "2 onions",
        "1 lb chicken breast",
    ]
    kept = {line.id: line for line in lines}
    assert kept[first_id].ingredient_id == spaghetti.id
    assert kept[first_id].match_confidence == pytest.approx(0.42)
    assert kept[second_id].ingredient_id == onion.id
    assert kept[second_id].match_confidence == pytest.approx(0.17)
    changed = next(line for line in lines if line.id not in {first_id, second_id})
    assert changed.ingredient is not None
    assert changed.ingredient.name == "chicken breast"


def test_editing_a_planned_recipe_updates_the_shopping_list(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    created = signed_in.post(
        "/recipes",
        data={
            "title": "Planned stew",
            "servings": "4",
            "ingredients": "1 pound spaghetti\n2 onions",
            "method": "Simmer.",
        },
    )
    assert created.status_code == 303
    seeded_session.expire_all()
    recipe = seeded_session.scalar(select(Recipe).where(Recipe.title == "Planned stew"))
    assert recipe is not None
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id,
            plan_date=date.today(),
            slot=MealSlot.DINNER,
            recipe_id=recipe.id,
            servings=4,
        )
    )
    seeded_session.commit()

    before = signed_in.get("/shopping").text
    assert "spaghetti" in before
    assert "yellow onion" in before
    assert "chicken breast" not in before

    edited = signed_in.post(
        f"/recipes/{recipe.id}",
        data={
            "title": "Planned stew",
            "servings": "4",
            "ingredients": "1 pound spaghetti\n1 lb chicken breast",
            "method": "Simmer.",
        },
    )
    assert edited.status_code == 303
    after = signed_in.get("/shopping").text
    assert "spaghetti" in after
    assert "chicken breast" in after
    assert "yellow onion" not in after


def test_archive_hides_the_recipe_and_undo_restores_it(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    from souschef.planning.suggest import suggest

    recipe = Recipe(
        user_id=web_user.id,
        title="Archive Me Stew",
        servings=2,
        source=RecipeSource.MANUAL,
        last_cooked_at=datetime(2024, 6, 1, tzinfo=UTC),
    )
    seeded_session.add(recipe)
    seeded_session.flush()
    entry = MealPlanEntry(
        user_id=web_user.id,
        plan_date=date.today(),
        slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=2,
    )
    cook = Cook(
        user_id=web_user.id,
        recipe_id=recipe.id,
        servings=2,
        cooked_at=datetime.now(UTC),
    )
    seeded_session.add_all([entry, cook])
    seeded_session.commit()
    assert recipe.title in signed_in.get("/recipes").text
    assert recipe.title in _row_titles(signed_in.get("/pick/recipe").text)

    archived = signed_in.post(f"/recipes/{recipe.id}/archive")
    assert archived.status_code == 303
    page = signed_in.get(archived.headers["location"])
    assert page.status_code == 200
    assert 'class="toast"' in page.text
    assert "Undo" in page.text
    assert f'action="/recipes/{recipe.id}/restore"' in page.text
    list_html = page.text.split('id="recipe-list"', 1)[1]
    assert recipe.title not in list_html
    assert recipe.title not in _row_titles(signed_in.get("/pick/recipe").text)
    assert recipe.title not in signed_in.get("/pick/recipe", params={"q": "Archive Me"}).text

    seeded_session.expire_all()
    assert all(item.recipe.id != recipe.id for item in suggest(seeded_session, web_user.id))
    assert seeded_session.get(MealPlanEntry, entry.id) is not None
    assert seeded_session.get(Cook, cook.id) is not None
    assert seeded_session.get(Recipe, recipe.id).is_archived is True
    plan = signed_in.get("/plan").text
    assert f'value="r:{recipe.id}"' not in plan
    assert recipe.title in plan

    restored = signed_in.post(f"/recipes/{recipe.id}/restore")
    assert restored.status_code == 303
    seeded_session.expire_all()
    assert seeded_session.get(Recipe, recipe.id).is_archived is False
    assert recipe.title in signed_in.get("/recipes").text
    assert any(item.recipe.id == recipe.id for item in suggest(seeded_session, web_user.id))


def test_a_blocked_import_offers_the_paste_box(
    monkeypatch: pytest.MonkeyPatch, signed_in: TestClient
) -> None:
    from souschef.config import get_settings
    from souschef.ingest.recipes import importers

    class Response:
        status_code = 403
        content = b""
        text = ""
        url = "https://example.com/blocked"

    monkeypatch.setattr(importers.httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(get_settings(), "no_external", False)
    page = signed_in.post("/recipes/import", data={"url": "https://example.com/blocked"})
    assert page.status_code == 200
    assert "That site blocks automated imports." in page.text
    assert "Paste the recipe text instead" in page.text
    assert 'name="text"' in page.text
    assert 'action="/recipes/paste"' in page.text
    assert "<details" not in page.text
