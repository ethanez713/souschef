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
    MealPlanEntry,
    MealSlot,
    Recipe,
    RecipeSource,
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
        assert response.headers["location"] == "/login"


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


def test_missing_recipe_is_404(signed_in: TestClient) -> None:
    assert signed_in.get("/recipes/99999").status_code == 404


def test_marking_cooked_debits_the_pantry(
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

    response = signed_in.post(f"/recipes/{recipe.id}/cooked", data={"servings": 1})
    assert response.status_code == 303

    seeded_session.expire_all()
    item = get_or_create_item(seeded_session, web_user.id, oil)
    assert item.quantity < 500
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 1


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
