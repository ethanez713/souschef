"""Pantry page: the item sheet, section order, and adding another."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import get_or_create_item, pantry_view, physical_of, record_event
from souschef.core.units import to_base
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    PantryItem,
    StorageLocation,
    User,
)


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None
    return found


def _pantry_row(session: Session, user: User, name: str) -> PantryItem | None:
    session.expire_all()
    ingredient = ing(session, name)
    return session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == user.id,
            PantryItem.ingredient_id == ingredient.id,
        )
    )


def _form(page: str, action: str) -> str:
    marker = f'action="{action}"'
    start = page.index(marker)
    return page[start : page.index("</form>", start)]


def _input_value(form: str, name: str) -> str:
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', form)
    assert match is not None, name
    return match.group(1)


def _headers(page: str) -> list[str]:
    return re.findall(r'<h2 class="group-head"><span>([^<]+)', page)


def _snapshot(session: Session, user_id: int) -> list[tuple]:
    session.expire_all()
    return [
        (
            view.ingredient.id,
            view.quantity,
            view.low,
            view.high,
            view.is_known,
            view.is_staple,
            str(view.item.location),
            view.item.restock_threshold,
        )
        for view in pantry_view(session, user_id, include_empty=True)
    ]


def _same_view(before: list[tuple], after: list[tuple]) -> None:
    assert len(after) == len(before)
    for left, right in zip(before, after, strict=True):
        assert right[0] == left[0]
        assert right[1] == pytest.approx(left[1], abs=1e-6)
        assert right[2] == pytest.approx(left[2], rel=1e-4, abs=1e-3)
        assert right[3] == pytest.approx(left[3], abs=1e-6)
        assert right[4:] == left[4:]


def test_threw_out_undo_leaves_the_pantry_view_unchanged(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    garlic = ing(seeded_session, "garlic")
    eggs = ing(seeded_session, "eggs")
    record_event(seeded_session, web_user.id, garlic, reason=EventReason.PURCHASE, delta=4)
    record_event(seeded_session, web_user.id, eggs, reason=EventReason.PURCHASE, delta=6)
    seeded_session.commit()
    before = _snapshot(seeded_session, web_user.id)

    posted = signed_in.post(f"/pantry/{garlic.id}/discard")
    assert posted.status_code == 303
    page = signed_in.get(posted.headers["location"]).text
    assert "Threw out garlic" in page
    event_id = int(posted.headers["location"].rsplit("changed=", 1)[1])
    assert f'action="/pantry/undo/{event_id}"' in page
    assert _pantry_row(seeded_session, web_user, "garlic").quantity == 0

    undone = signed_in.post(f"/pantry/undo/{event_id}")
    assert undone.status_code == 303
    _same_view(before, _snapshot(seeded_session, web_user.id))


def test_keep_at_least_stores_base_units_and_clear_unsets_it(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    record_event(seeded_session, web_user.id, chicken, reason=EventReason.PURCHASE, delta=4.5)
    seeded_session.commit()

    page = signed_in.get("/pantry").text
    floor = _form(page, f"/pantry/{chicken.id}/floor")
    assert _input_value(floor, "unit") == "oz"
    assert _input_value(floor, "quantity") == ""

    posted = signed_in.post(
        f"/pantry/{chicken.id}/floor", data={"quantity": "2", "unit": "oz"}
    )
    assert posted.status_code == 303
    kept = signed_in.get(posted.headers["location"]).text
    assert "Keep at least 2 oz chicken thigh" in kept
    stored = _pantry_row(seeded_session, web_user, "chicken thigh")
    assert stored is not None
    assert stored.restock_threshold == pytest.approx(to_base(2, "oz", physical_of(chicken)))

    cleared = signed_in.post(f"/pantry/{chicken.id}/floor", data={"clear": "1"})
    assert cleared.status_code == 303
    cleared_page = signed_in.get(cleared.headers["location"]).text
    assert "Cleared the minimum for chicken thigh" in cleared_page
    assert _pantry_row(seeded_session, web_user, "chicken thigh").restock_threshold is None


def test_a_cook_only_ingredient_is_not_listed(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    lemon = ing(seeded_session, "lemon")
    eggs = ing(seeded_session, "eggs")
    record_event(seeded_session, web_user.id, eggs, reason=EventReason.PURCHASE, delta=6)
    record_event(seeded_session, web_user.id, lemon, reason=EventReason.COOK, delta=-1)
    seeded_session.commit()

    page = signed_in.get("/pantry").text
    assert "eggs" in page
    assert f'action="/pantry/{lemon.id}/check"' not in page
    assert 'class="list-row-title">lemon' not in page
    assert "<span>Out</span>" not in page


def test_garlic_added_fresh_lands_in_the_pantry(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    added = signed_in.post(
        "/pantry/add", data={"name": "garlic", "quantity": "3", "unit": "clove"}
    )
    assert added.status_code == 303
    assert _pantry_row(seeded_session, web_user, "garlic").location == StorageLocation.PANTRY

    for name in ("yellow onion", "red onion", "shallot", "potato", "sweet potato"):
        record_event(
            seeded_session, web_user.id, ing(seeded_session, name),
            reason=EventReason.PURCHASE, delta=1,
        )
    for name in ("carrot", "scallion"):
        record_event(
            seeded_session, web_user.id, ing(seeded_session, name),
            reason=EventReason.PURCHASE, delta=1,
        )
    seeded_session.commit()

    for name in ("yellow onion", "red onion", "shallot", "potato", "sweet potato"):
        assert _pantry_row(seeded_session, web_user, name).location == StorageLocation.PANTRY
    for name in ("carrot", "scallion"):
        assert _pantry_row(seeded_session, web_user, name).location == StorageLocation.FRIDGE


def test_an_existing_location_is_left_alone(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    potato = ing(seeded_session, "potato")
    get_or_create_item(seeded_session, web_user.id, potato, location=StorageLocation.FRIDGE)
    seeded_session.commit()

    added = signed_in.post(
        "/pantry/add", data={"name": "potato", "quantity": "2", "unit": "potato"}
    )
    assert added.status_code == 303
    row = _pantry_row(seeded_session, web_user, "potato")
    assert row is not None
    assert row.location == StorageLocation.FRIDGE
    assert row.quantity == pytest.approx(2)


def test_section_order_stays_put_after_a_check(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    eggs = ing(seeded_session, "eggs")
    peas = ing(seeded_session, "frozen peas")
    beans = ing(seeded_session, "black beans")
    carrot = ing(seeded_session, "carrot")
    record_event(seeded_session, web_user.id, eggs, reason=EventReason.PURCHASE, delta=6)
    record_event(seeded_session, web_user.id, peas, reason=EventReason.PURCHASE, delta=200)
    record_event(seeded_session, web_user.id, beans, reason=EventReason.PURCHASE, delta=2)
    record_event(seeded_session, web_user.id, carrot, reason=EventReason.MANUAL, level=0)
    seeded_session.commit()

    order = ["Fridge", "Freezer", "Pantry", "Out"]
    page = signed_in.get("/pantry").text
    assert _headers(page) == order

    form = _form(page, f"/pantry/{eggs.id}/check")
    shown = _input_value(form, "shown")
    posted = signed_in.post(
        f"/pantry/{eggs.id}/check",
        data={"quantity": shown, "shown": shown},
        headers={"HX-Request": "true"},
    )
    assert posted.status_code == 303
    fragment = signed_in.get(posted.headers["location"], headers={"HX-Request": "true"}).text
    assert _headers(fragment) == order
    assert "Scan receipt" not in fragment
    check = _form(fragment, f"/pantry/{eggs.id}/check")
    assert "/discard" not in check
    for item in (eggs, peas, beans, carrot):
        assert f'data-sheet="pantry-{item.id}"' in fragment
        assert f'id="pantry-{item.id}"' in fragment


def test_htmx_search_keeps_the_item_sheet(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    thigh = ing(seeded_session, "chicken thigh")
    record_event(seeded_session, web_user.id, thigh, reason=EventReason.PURCHASE, delta=200)
    seeded_session.commit()

    fragment = signed_in.get(
        "/pantry", params={"q": "chicken"}, headers={"HX-Request": "true"}
    ).text
    assert "Scan receipt" not in fragment
    assert f'data-sheet="pantry-{thigh.id}"' in fragment
    assert f'id="pantry-{thigh.id}"' in fragment
    assert 'name="q" value="chicken"' in fragment
    assert f'action="/pantry/{thigh.id}/discard"' in fragment


def test_add_another_toasts_each_purchase_and_keeps_the_sheet_open(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    def add_two() -> str:
        posted = signed_in.post(
            "/pantry/add",
            data={"name": "chicken thigh", "quantity": "2", "unit": "lb", "again": "1"},
        )
        assert posted.status_code == 303
        location = posted.headers["location"]
        assert location.index("again=1") < location.index("changed=")
        page = signed_in.get(location).text
        assert "Added 2 lb chicken thigh" in page
        dialog = re.search(r'<dialog class="([^"]*)" id="pantry-add"([^>]*)>', page)
        assert dialog is not None
        assert "sheet-stay" in dialog.group(1)
        assert " open" in dialog.group(2)
        assert "data-open" not in dialog.group(2)
        start = page.index("data-add-form")
        form = page[start : page.index("</form>", start)]
        assert _input_value(form, "name") == ""
        assert _input_value(form, "quantity") == "1"
        return page

    add_two()
    add_two()

    thigh = ing(seeded_session, "chicken thigh")
    seeded_session.expire_all()
    events = seeded_session.scalars(
        select(InventoryEvent).where(
            InventoryEvent.user_id == web_user.id,
            InventoryEvent.ingredient_id == thigh.id,
            InventoryEvent.reason == EventReason.PURCHASE,
        )
    ).all()
    assert len(events) == 2
    stored = _pantry_row(seeded_session, web_user, "chicken thigh")
    assert stored is not None
    assert stored.quantity == pytest.approx(to_base(4, "lb", physical_of(thigh)))


def test_always_have_collapses_the_row_and_clears_the_floor(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    record_event(seeded_session, web_user.id, chicken, reason=EventReason.PURCHASE, delta=200)
    get_or_create_item(seeded_session, web_user.id, chicken).restock_threshold = 100
    seeded_session.commit()

    posted = signed_in.post(f"/pantry/{chicken.id}/staple")
    assert posted.status_code == 303
    page = signed_in.get(posted.headers["location"]).text
    assert "Always have chicken thigh" in page
    assert 'class="list-row pantry-always" href="/settings"' in page
    assert "Always have (1)" in page
    assert f'action="/pantry/{chicken.id}/check"' not in page

    seeded_session.expire_all()
    assert ing(seeded_session, "chicken thigh").is_staple is True
    assert _pantry_row(seeded_session, web_user, "chicken thigh").restock_threshold is None
