"""Shopping-list amounts and recipe-list gaps, as the rows actually read."""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import physical_of, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.core.units import to_base
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    RecipeSource,
    ShoppingListItem,
    User,
)
from souschef.planning.shopping import listed_purchase


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None
    return found


def _save(session: Session, user: User, title: str, ingredients: list[str]):
    return save_imported_recipe(
        session,
        user.id,
        RawRecipe(
            title=title,
            ingredients=ingredients,
            servings=1,
            source_url=f"https://example.com/{title.replace(' ', '-').lower()}",
        ),
        source=RecipeSource.URL,
    )


def _plan(session: Session, user: User, title: str, ingredients: list[str]) -> None:
    recipe = _save(session, user, title, ingredients)
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


def _shop_item_id(html: str, label: str) -> int:
    marker = re.search(rf">\s*{re.escape(label)}\s*<", html)
    assert marker is not None, label
    window = html[max(0, marker.start() - 1200) : marker.start()]
    found = re.findall(r'action="/shopping/item/(\d+)/check"', window)
    assert found, label
    return int(found[-1])


def _input_value(html: str, element_id: str) -> str:
    match = re.search(rf'id="{element_id}"[^>]*value="([^"]*)"', html)
    assert match is not None, element_id
    return match.group(1)


def _purchase_base(ingredient: CanonicalIngredient, item: ShoppingListItem) -> float:
    qty, unit = listed_purchase(ingredient, item.purchase_qty, item.purchase_unit)
    return to_base(qty, unit, physical_of(ingredient))


def _recipe_row(page: str, title: str) -> str:
    start = page.index(f'class="list-row-title">{title}<')
    return page[start : page.index("</a>", start)]


def _library(session: Session, user: User) -> None:
    """One recipe in each list group: ready, a few gaps, and more than three."""
    _save(session, user, "Weeknight Pasta", ["1 pound spaghetti"])
    record_event(
        session,
        user.id,
        ing(session, "spaghetti"),
        reason=EventReason.PURCHASE,
        delta=2000,
    )
    _save(session, user, "Garlic Bread", ["3 cloves garlic"])
    _save(
        session,
        user,
        "Sunday Stew",
        [
            "3 cloves garlic",
            "1 onion",
            "1 pound chicken breast",
            "1 tablespoon butter",
            "1 cup xyzzybroth",
        ],
    )
    session.commit()


def test_two_onions_read_as_onions(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan(
        seeded_session,
        web_user,
        "Onion Soup",
        ["2 yellow onions", "1 red onion"],
    )

    page = signed_in.get("/shopping").text

    assert '<span class="num">2 onions</span>' in page
    assert '<span class="num">1 onion</span>' in page
    assert "2×" not in page
    assert "1×" not in page
    assert " × " not in page


def test_wine_reads_as_a_bottle(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    wines = seeded_session.scalars(
        select(CanonicalIngredient)
        .where(CanonicalIngredient.aisle == "Beer & Wine")
        .order_by(CanonicalIngredient.name)
    ).all()
    assert [row.name for row in wines] == ["dry red wine", "dry white wine"]
    assert [row.package_noun for row in wines] == ["bottle", "bottle"]

    _plan(
        seeded_session,
        web_user,
        "Pan Sauce",
        ["1 cup dry white wine", "1 cup dry red wine"],
    )

    page = signed_in.get("/shopping").text

    assert page.count('<span class="num">1 bottle</span>') == 2
    assert "3⅛" not in page
    assert " × " not in page


def test_black_beans_sheet_unit_round_trips(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan(seeded_session, web_user, "Beans", ["2 cans black beans"])
    page = signed_in.get("/shopping").text
    assert '<span class="num">2 cans</span>' in page

    item_id = _shop_item_id(page, "black beans")
    amount = _input_value(page, f"shop-amt-{item_id}")
    unit = _input_value(page, f"shop-unit-{item_id}")
    assert amount == "2"
    assert unit == "can"

    seeded_session.expire_all()
    beans = ing(seeded_session, "black beans")
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    before = _purchase_base(beans, item)

    posted = signed_in.post(
        f"/shopping/item/{item_id}/amount",
        data={"amount": amount, "unit": unit},
    )
    assert posted.status_code == 303

    seeded_session.expire_all()
    item = seeded_session.get(ShoppingListItem, item_id)
    assert item is not None
    assert _purchase_base(beans, item) == pytest.approx(before)

    again = signed_in.get("/shopping").text
    assert '<span class="num">2 cans</span>' in again
    assert _input_value(again, f"shop-unit-{item_id}") == "can"


def test_a_recipe_row_names_what_is_missing(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _library(seeded_session, web_user)

    page = signed_in.get("/recipes").text
    ready = _recipe_row(page, "Weeknight Pasta")
    few = _recipe_row(page, "Garlic Bread")
    stew = _recipe_row(page, "Sunday Stew")

    assert 'class="list-row-sub">Ready to cook<' in ready
    assert 'class="badge good">Ready<' in ready
    assert 'class="list-row-sub">Missing 1: garlic<' in few
    assert 'class="badge warn">Missing 1<' in few
    assert (
        'class="list-row-sub">Missing 4: garlic, yellow onion, chicken breast'
        " +1 more · 1 needs naming<"
    ) in stew
    assert 'class="badge warn">Missing 5<' in stew
    assert "not identified" not in page
    assert "unidentified" not in page
    assert re.search(r"Needs \d+ things", page) is None


def test_recipe_groups_read_ready_missing_a_few_and_missing_more(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _library(seeded_session, web_user)

    recipes = signed_in.get("/recipes").text
    plan = signed_in.get("/plan").text

    assert "<span>Ready</span>" in recipes
    assert "<span>Missing 1–3</span>" in recipes
    assert "<span>Missing more</span>" in recipes
    assert "Missing 4+" not in recipes
    assert "<span>Ready</span>" in plan
    assert "<span>Missing 1–3</span>" in plan
    assert "<span>Missing more</span>" in plan
    assert "Missing 4+" not in plan
