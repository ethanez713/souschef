"""Check first is the one place the list asks about stock."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

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
    InventoryEvent,
    MealPlanEntry,
    MealSlot,
    PantryItem,
    RecipeSource,
    User,
)
from souschef.planning.shopping import check_first_rows
from souschef.web.common import _horizon


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None, name
    return found


def _plan(session: Session, user: User, title: str, ingredients: list[str], url: str) -> None:
    recipe = save_imported_recipe(
        session,
        user.id,
        RawRecipe(title=title, ingredients=ingredients, servings=1, source_url=url),
        source=RecipeSource.URL,
    )
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


def _events(session: Session, user_id: int, ingredient_id: int) -> list[InventoryEvent]:
    return list(
        session.scalars(
            select(InventoryEvent)
            .where(
                InventoryEvent.user_id == user_id,
                InventoryEvent.ingredient_id == ingredient_id,
            )
            .order_by(InventoryEvent.id)
        )
    )


def _shop_item_id(html: str, label: str) -> int:
    marker = re.search(rf">\s*{re.escape(label)}\s*<", html)
    assert marker is not None, label
    window = html[max(0, marker.start() - 1200) : marker.start()]
    found = re.findall(r'action="/shopping/item/(\d+)/check"', window)
    assert found, label
    return int(found[-1])


def test_have_it_on_an_unknown_writes_one_manual_and_drops_the_row(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    _plan(
        seeded_session,
        web_user,
        "Thighs",
        ["200 g chicken thigh"],
        "https://example.com/check-have",
    )

    page = signed_in.get("/shopping").text
    assert "Check first (1)" in page
    assert f'action="/shopping/check/{chicken.id}/have"' in page
    assert 'action="/shopping/item/' not in page

    posted = signed_in.post(f"/shopping/check/{chicken.id}/have")
    assert posted.status_code == 303

    seeded_session.expire_all()
    events = _events(seeded_session, web_user.id, chicken.id)
    assert len(events) == 1
    assert events[0].reason == EventReason.MANUAL
    package = to_base(
        float(chicken.package_qty), chicken.package_unit, physical_of(chicken)
    )
    assert events[0].resulting_quantity == pytest.approx(max(200, package))

    after = signed_in.get("/shopping").text
    assert f'action="/shopping/check/{chicken.id}/have"' not in after
    assert "Check first" not in after
    assert "chicken thigh" not in after


def test_need_it_writes_manual_zero_and_the_line_survives_a_rebuild(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    _plan(
        seeded_session,
        web_user,
        "Thighs",
        ["200 g chicken thigh"],
        "https://example.com/check-need",
    )

    posted = signed_in.post(f"/shopping/check/{chicken.id}/need")
    assert posted.status_code == 303

    seeded_session.expire_all()
    events = _events(seeded_session, web_user.id, chicken.id)
    assert [(event.reason, event.resulting_quantity) for event in events] == [
        (EventReason.MANUAL, 0)
    ]

    page = signed_in.get("/shopping").text
    item_id = _shop_item_id(page, "chicken thigh")
    assert f'action="/shopping/check/{chicken.id}/need"' not in page

    signed_in.post("/shopping/generate")
    again = signed_in.get("/shopping").text
    assert _shop_item_id(again, "chicken thigh") == item_id
    assert "chicken thigh" in again


def test_still_right_on_an_audit_row_writes_confirm_and_drops_it(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    rice = ing(seeded_session, "white rice")
    record_event(seeded_session, web_user.id, rice, reason=EventReason.PURCHASE, delta=500)
    item = seeded_session.scalar(select(PantryItem).where(PantryItem.ingredient_id == rice.id))
    assert item is not None
    item.belief_at = datetime.now(UTC) - timedelta(days=90)
    _plan(
        seeded_session,
        web_user,
        "Rice Bowl",
        ["480 g white rice"],
        "https://example.com/check-still",
    )

    start, end = _horizon()
    rows = check_first_rows(seeded_session, web_user.id, covers_from=start, covers_to=end)
    assert [row.ingredient.id for row in rows] == [rice.id]
    assert rows[0].kind.value == "audit"

    page = signed_in.get("/shopping").text
    assert "Check first (1)" in page
    assert f'action="/shopping/check/{rice.id}/still"' in page
    assert "Still right" in page

    posted = signed_in.post(f"/shopping/check/{rice.id}/still")
    assert posted.status_code == 303

    seeded_session.expire_all()
    reasons = [event.reason for event in _events(seeded_session, web_user.id, rice.id)]
    assert reasons == [EventReason.PURCHASE, EventReason.CONFIRM]

    after = signed_in.get("/shopping").text
    assert f'action="/shopping/check/{rice.id}/still"' not in after
    assert "Check first" not in after
    assert "white rice" not in after


def test_the_audit_page_is_gone(signed_in: TestClient) -> None:
    assert signed_in.get("/audit").status_code == 404
