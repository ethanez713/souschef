"""The receipt review: compact rows, picking an item, adding a line, applying."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import get_or_create_item
from souschef.models import (
    CanonicalIngredient,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
)


def _ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None, name
    return found


def _receipt(session: Session, user_id: int, *lines: ReceiptLine) -> Receipt:
    receipt = Receipt(
        user_id=user_id, image_path="test.jpg", status=ReceiptStatus.NEEDS_REVIEW,
        ocr_confidence=0.95,
    )
    session.add(receipt)
    session.flush()
    for position, line in enumerate(lines):
        line.receipt_id = receipt.id
        line.position = position
    session.add_all(lines)
    session.commit()
    return receipt


def test_review_page_has_compact_rows_and_no_scan_grades(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    garlic = _ing(seeded_session, "garlic")
    receipt = _receipt(
        seeded_session, web_user.id,
        ReceiptLine(raw_text="GARLIC", ingredient_id=garlic.id, match_confidence=0.6,
                    parsed_quantity=1, parsed_unit="count", is_confirmed=True),
        ReceiptLine(raw_text="MYSTERY ITEM 4.99"),
    )

    page = signed_in.get(f"/receipts/{receipt.id}").text

    assert "<select" not in page
    assert "needs a look" not in page
    assert "scan quality" not in page
    assert "Pick item" in page
    assert "Add 1 to pantry" in page


def test_picking_an_item_on_an_unmatched_line_stocks_it(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    rice = _ing(seeded_session, "white rice")
    line = ReceiptLine(raw_text="JASMINE 2LB")
    receipt = _receipt(seeded_session, web_user.id, line)
    before = get_or_create_item(seeded_session, web_user.id, rice).quantity
    seeded_session.commit()

    applied = signed_in.post(
        f"/receipts/{receipt.id}/apply",
        data={f"ingredient_{line.id}": str(rice.id), f"quantity_{line.id}": "2",
              f"unit_{line.id}": "lb"},
    )

    assert applied.status_code == 200
    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, rice).quantity > before


def test_an_added_line_is_stocked_on_apply(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    eggs = _ing(seeded_session, "eggs")
    receipt = _receipt(seeded_session, web_user.id)
    before = get_or_create_item(seeded_session, web_user.id, eggs).quantity
    seeded_session.commit()

    added = signed_in.post(
        f"/receipts/{receipt.id}/add-line",
        data={"new_ingredient": str(eggs.id), "new_quantity": "12", "new_unit": "count"},
    )
    assert added.status_code == 303
    seeded_session.expire_all()
    new_line = seeded_session.scalar(
        select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.id)
    )
    assert new_line is not None and new_line.is_confirmed

    applied = signed_in.post(
        f"/receipts/{receipt.id}/apply", data={f"confirmed_{new_line.id}": "on"}
    )

    assert applied.status_code == 200
    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, eggs).quantity == before + 12


def test_adding_a_line_keeps_the_rows_edited_so_far(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    garlic = _ing(seeded_session, "garlic")
    eggs = _ing(seeded_session, "eggs")
    line = ReceiptLine(raw_text="GARLIC", ingredient_id=garlic.id, parsed_quantity=1,
                       parsed_unit="count", is_confirmed=True)
    receipt = _receipt(seeded_session, web_user.id, line)

    signed_in.post(
        f"/receipts/{receipt.id}/add-line",
        data={f"ingredient_{line.id}": str(garlic.id), f"quantity_{line.id}": "3",
              f"unit_{line.id}": "count", f"confirmed_{line.id}": "on",
              "new_ingredient": str(eggs.id)},
    )

    seeded_session.expire_all()
    assert seeded_session.get(ReceiptLine, line.id).parsed_quantity == 3


def test_adding_a_line_without_an_item_is_refused(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    receipt = _receipt(seeded_session, web_user.id)

    response = signed_in.post(f"/receipts/{receipt.id}/add-line", data={"new_quantity": "2"})

    assert response.status_code == 400
    assert "Pick an item" in response.text
