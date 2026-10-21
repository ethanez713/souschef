"""Invented receipts as the OCR payload the receipt parser consumes.

Every value is made up. The phone is 555-0100, the ZIP is 00000, and the card
line ends in 0000. ``rapidocr_payload`` is the ``[polygon, text, score]``
triples RapidOCR emits; ``ocr_result`` is what ``parse_receipt`` consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from souschef.ingest.ocr import OcrResult, _to_boxes, group_boxes_into_lines

# group_boxes_into_lines joins boxes whose centres fall within 0.6 of a
# character height. Rows are a full step apart so a wrapped continuation
# stays on its own line.
_ROW_HEIGHT = 20.0
_ROW_STEP = 32.0
_X = 12.0
_CHAR = 8.0
_GAP = 18.0
_SCORE = 0.96

_PRICE = re.compile(r"\$?(\d*\.\d{2})(?!\d)")


@dataclass(frozen=True)
class ItemSpec:
    text: str
    price: float
    continuation: str | None = None
    fragments: tuple[str, ...] = ()
    score: float = _SCORE
    continuation_score: float = _SCORE


@dataclass(frozen=True)
class ReceiptSpec:
    store: str
    items: tuple[ItemSpec, ...]
    subtotal: float
    tax: float
    total: float
    purchased_on: date
    phone: str = "555-0100"
    zip_code: str = "00000"
    card_last4: str = "0000"
    street: str = "123 Main St"
    city: str = "Anytown"
    state: str = "CA"
    clock: str = "09:15"
    total_label: str = "TOTAL"


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def _validate(spec: ReceiptSpec) -> None:
    item_cents = sum(_cents(item.price) for item in spec.items)
    if not spec.items or item_cents != _cents(spec.subtotal):
        raise ValueError("item prices do not add up to the subtotal")
    if _cents(spec.subtotal) + _cents(spec.tax) != _cents(spec.total):
        raise ValueError("subtotal plus tax does not equal the total")
    for item in spec.items:
        if item.fragments and " ".join(item.fragments) != item.text:
            raise ValueError(f"fragments do not join to {item.text!r}")
        if item.continuation is None:
            continue
        prices = _PRICE.findall(item.continuation)
        if not prices or _cents(float(prices[-1])) != _cents(item.price):
            raise ValueError(f"continuation price does not match {item.price:.2f}")


def _money(amount: float) -> str:
    return f"{amount:.2f}"


def _item_rows(item: ItemSpec) -> list[list[tuple[str, float]]]:
    if item.continuation is not None:
        parts = item.fragments or (item.text,)
        return [
            [(part, item.score) for part in parts],
            [(item.continuation, item.continuation_score)],
        ]
    parts = list(item.fragments or (item.text,))
    return [[(part, item.score) for part in parts] + [(_money(item.price), item.score)]]


def _rows(spec: ReceiptSpec) -> list[list[tuple[str, float]]]:
    rows: list[list[tuple[str, float]]] = [
        [(spec.store, _SCORE)],
        [(spec.street, _SCORE)],
        [(f"{spec.city} {spec.state} {spec.zip_code}", _SCORE)],
        [(spec.phone, _SCORE)],
        [(f"{spec.purchased_on:%m/%d/%Y} {spec.clock}", _SCORE)],
    ]
    for item in spec.items:
        rows.extend(_item_rows(item))
    rows.extend(
        [
            [(f"SUBTOTAL {_money(spec.subtotal)}", _SCORE)],
            [(f"TAX {_money(spec.tax)}", _SCORE)],
            [(f"{spec.total_label} {_money(spec.total)}", _SCORE)],
            [(f"VISA **** {spec.card_last4}", _SCORE)],
        ]
    )
    return rows


def _polygon(x: float, y: float, text: str) -> list[list[float]]:
    width = max(_CHAR * len(text), _CHAR)
    bottom = y + _ROW_HEIGHT
    return [[x, y], [x + width, y], [x + width, bottom], [x, bottom]]


def rapidocr_payload(spec: ReceiptSpec) -> list[list[object]]:
    """RapidOCR ``[polygon, text, score]`` triples, top to bottom."""
    _validate(spec)
    payload: list[list[object]] = []
    y = 40.0
    for row in _rows(spec):
        x = _X
        for text, score in row:
            payload.append([_polygon(x, y, text), text, score])
            x += _CHAR * len(text) + _GAP
        y += _ROW_STEP
    return payload


def ocr_result(spec: ReceiptSpec) -> OcrResult:
    """Group a synthetic receipt into the lines ``parse_receipt`` reads."""
    lines = group_boxes_into_lines(_to_boxes(rapidocr_payload(spec)))
    return OcrResult(lines=lines, engine="rapidocr")
