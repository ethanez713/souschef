"""Receipt ingest on invented OCR payloads.

Store, item lines, totals and the purchase date are read back from generated
scans. Card, phone and settlement rows stay noise, so they are not pantry items.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session
from tests.fixtures.synthetic_receipts import ItemSpec, ReceiptSpec, ocr_result

from souschef.ingest.receipt import (
    ParsedReceiptLine,
    detect_store,
    detect_total,
    parse_receipt,
    should_preconfirm,
)

CLEAN = ReceiptSpec(
    store="Corner Market",
    items=(
        ItemSpec("BANANAS", 2.00),
        ItemSpec("LG EGGS GRADE A 12CT", 4.19),
        ItemSpec("ORG BABY SPINACH 5OZ", 3.99),
    ),
    subtotal=10.18,
    tax=0.81,
    total=10.99,
    purchased_on=date(2026, 3, 14),
    phone="555-0100",
    zip_code="00000",
    card_last4="0000",
    street="123 Main St",
    city="Anytown",
    state="CA",
    clock="09:15",
)

# A description split across boxes, a weight row under it, a size read as
# "80Z", and a total whose O was read as zero.
MESSY = ReceiptSpec(
    store="Corner Market",
    items=(
        ItemSpec(
            "BNLS SKLS CHKN BRST",
            11.68,
            continuation="2.34 lb @ 4.99/lb 11.68",
            fragments=("BNLS SKLS", "CHKN BRST"),
            continuation_score=0.80,
        ),
        ItemSpec("SHRD MOZZ CHZ 80Z", 3.29, score=0.84),
    ),
    subtotal=14.97,
    tax=1.20,
    total=16.17,
    purchased_on=date(2026, 3, 14),
    phone="555-0100",
    zip_code="00000",
    card_last4="0000",
    total_label="T0TAL",
)


def _purchases(parsed: list[ParsedReceiptLine]) -> list[ParsedReceiptLine]:
    return [
        line
        for line in parsed
        if not line.is_noise and line.match is not None and line.match.is_match
    ]


def test_clean_receipt_yields_the_store_the_items_and_their_prices(
    seeded_session: Session,
) -> None:
    result = ocr_result(CLEAN)
    parsed = parse_receipt(result, seeded_session)

    assert detect_store(result.lines) == "CORNER MARKET"
    assert [
        (line.match.name, line.price, line.quantity, line.unit) for line in _purchases(parsed)
    ] == [
        ("banana", pytest.approx(2.00), None, None),
        ("eggs", pytest.approx(4.19), pytest.approx(12), "count"),
        ("spinach", pytest.approx(3.99), pytest.approx(5), "oz"),
    ]


def test_item_prices_tax_and_the_detected_total_agree(seeded_session: Session) -> None:
    result = ocr_result(CLEAN)
    parsed = parse_receipt(result, seeded_session)
    prices = [line.price for line in _purchases(parsed)]

    assert sum(price for price in prices if price is not None) == pytest.approx(CLEAN.subtotal)
    assert CLEAN.subtotal + CLEAN.tax == pytest.approx(CLEAN.total)
    assert detect_total(result.lines) == pytest.approx(CLEAN.total)


def test_the_purchase_date_parses_and_is_not_an_item(seeded_session: Session) -> None:
    result = ocr_result(CLEAN)
    parsed = parse_receipt(result, seeded_session)
    dated = [line for line in result.lines if "/" in line.text and ":" in line.text]

    assert len(dated) == 1
    assert datetime.strptime(dated[0].text, "%m/%d/%Y %H:%M").date() == CLEAN.purchased_on
    match = next(line for line in parsed if line.raw_text == dated[0].text)
    assert match.is_noise
    assert match.match is None
    assert not should_preconfirm(match)


def test_card_phone_and_totals_never_become_items(seeded_session: Session) -> None:
    parsed = parse_receipt(ocr_result(CLEAN), seeded_session)
    by_text = {line.raw_text: line for line in parsed}
    furniture = (
        "123 Main St",
        "Anytown CA 00000",
        "555-0100",
        "03/14/2026 09:15",
        "SUBTOTAL 10.18",
        "TAX 0.81",
        "TOTAL 10.99",
        "VISA **** 0000",
    )

    for text in furniture:
        line = by_text[text]
        assert line.is_noise
        assert line.match is None
        assert not should_preconfirm(line)
    header = by_text["Corner Market"]
    assert header.match is not None and not header.match.is_match
    assert not should_preconfirm(header)
    assert [line.match.name for line in _purchases(parsed)] == ["banana", "eggs", "spinach"]


def test_wrapped_weight_line_and_an_oz_misread_still_parse(seeded_session: Session) -> None:
    result = ocr_result(MESSY)
    parsed = parse_receipt(result, seeded_session)
    purchases = _purchases(parsed)

    assert detect_store(result.lines) == "CORNER MARKET"
    assert detect_total(result.lines) == pytest.approx(MESSY.total)
    assert [
        (line.match.name, line.price, line.quantity, line.unit) for line in purchases
    ] == [
        ("chicken breast", pytest.approx(11.68), pytest.approx(2.34), "lb"),
        ("mozzarella cheese", pytest.approx(3.29), pytest.approx(8), "oz"),
    ]
    chicken, cheese = purchases
    assert chicken.raw_text == "BNLS SKLS CHKN BRST 2.34 lb @ 4.99/lb 11.68"
    assert chicken.ocr_confidence == pytest.approx(0.80)
    assert cheese.raw_text == "SHRD MOZZ CHZ 80Z 3.29"
    assert {line.raw_text: line for line in parsed}["T0TAL 16.17"].is_noise
