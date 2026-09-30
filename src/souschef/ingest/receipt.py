"""Turn OCR output into reviewable pantry updates.

Nothing here writes to the pantry. Receipt OCR on thermal paper is unreliable
enough that applying it automatically would corrupt inventory faster than manual
entry ever could, so the output is a set of *proposals* the user confirms.

The work splits into three parts:

1. **Classify** each row. A receipt is mostly furniture -- store address, phone
   number, subtotal, tax, payment lines. Letting those reach the matcher fills
   the review screen with "TAX -> canned tuna" and buries the real items.
2. **Extract** quantity, weight and price from the row.
3. **Match** the remaining description to a canonical ingredient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from souschef.core.matching import IngredientMatcher, MatchResult, clean_receipt_line
from souschef.ingest.ocr import OcrLine, OcrResult

# Rows that are receipt furniture rather than purchases. Matched as whole words
# against the row, so "TOTAL" is dropped but "TOTAL WHEAT BREAD" survives.
_NOISE_PATTERNS = [
    r"\bsub\s*total\b", r"\btotal\b", r"\btax\b", r"\bchange\b", r"\bcash\b",
    r"\bcredit\b", r"\bdebit\b", r"\bvisa\b", r"\bmastercard\b", r"\bamex\b",
    r"\bbalance\b", r"\btender\b", r"\bpayment\b", r"\bauth\b", r"\bapproved\b",
    r"\bref\s*#", r"\bterminal\b", r"\bmerchant\b", r"\bcashier\b", r"\breg\b",
    r"\bstore\s*#", r"\bthank\s*you\b", r"\bsurvey\b", r"\bsavings\b",
    r"\bdiscount\b", r"\bcoupon\b", r"\bloyalty\b", r"\brewards\b", r"\bpoints\b",
    r"\breturn\b", r"\bpolicy\b", r"\breceipt\b", r"\bitems?\s+sold\b",
    r"\bphone\b", r"\bwww\.", r"\.com\b", r"\bst\b\s*$", r"\bave\b\s*$",
    r"\bopen\b", r"\bhours\b", r"\bmanager\b", r"\bbag\s*fee\b", r"\bdeposit\b",
]
_NOISE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)

# A card number, or the masked last digits stores print after VISA / CARD.
_CARD_NUMBER = re.compile(r"(?:\d[ -]?){13,19}")
_MASKED_CARD = re.compile(
    r"(?:\b(?:card|acct|account|visa|mastercard|amex|discover|debit|credit)\b[^0-9]{0,8})?\*{2,}\s*\d{3,4}",
    re.IGNORECASE,
)
_STATE_ZIP = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "IA", "ID",
    "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT",
    "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY", "DC",
})

# A row that is only digits, punctuation and currency is a number, not an item.
_ONLY_NUMERIC = re.compile(r"^[\s\d.,:$#*/x@-]+$")
# Dates and times.
_DATE_LIKE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{1,2}:\d{2}\b")
# Long digit runs: card fragments, barcodes, transaction ids.
_LONG_DIGITS = re.compile(r"\b\d{8,}\b")

_PRICE = re.compile(r"\$?(\d+\.\d{2})(?!\d)")
# "2.34 lb @ 4.99/lb" -- a weighed item.
_WEIGHED = re.compile(r"(\d+\.?\d*)\s*(lb|lbs|kg|oz|g)\b\s*@", re.IGNORECASE)
# "3 @ 1.99" or "3 x 1.99" -- a multi-buy.
_MULTIPLE = re.compile(r"^\s*(\d{1,2})\s*[@x]\s*\$?\d+\.\d{2}", re.IGNORECASE)
# A size embedded in the description: "MOZZ CHZ 8OZ", "MILK 1GAL".
_SIZE = re.compile(r"(\d+\.?\d*)\s*(z|oz|lb|lbs|g|kg|ml|l|ct|pk|gal|qt|pt)\b", re.IGNORECASE)

_SIZE_UNIT_ALIASES = {"z": "oz", "ct": "count", "pk": "count"}

# "8 OZ" reliably OCRs as "80Z" because O becomes zero. Read literally that is
# eighty ounces -- a tenfold error on every packaged good. A trailing zero
# immediately before a lone Z is always the letter, since no receipt prints a
# size in "160 z".
_OZ_CONFUSION = re.compile(r"\b(\d+(?:\.\d+)?)0Z\b", re.IGNORECASE)

# A row carrying only a weight and prices: the continuation of the item above.
_CONTINUATION = re.compile(
    r"^\s*\d+\.?\d*\s*(?:lb|lbs|kg|oz|g)\b.*@|^\s*\d+\s*[@x]\s*\$?\d+\.\d{2}",
    re.IGNORECASE,
)


def repair_size_confusions(text: str) -> str:
    """Undo the O/zero confusion in printed sizes before parsing them."""
    return _OZ_CONFUSION.sub(lambda m: f"{m.group(1)} OZ", text)


def is_continuation(text: str) -> bool:
    """True for a weight/price row that belongs to the description above it.

    Receipts print weighed goods across two rows -- "BNLS SKLS CHKN BRST" then
    "2.34 lb @ 4.99/lb  11.68". Parsed independently, the item gets no quantity
    and the weight row looks like junk, so they have to be rejoined.
    """
    return bool(_CONTINUATION.match(text.strip()))


@dataclass
class ParsedReceiptLine:
    raw_text: str
    description: str
    quantity: float | None
    unit: str | None
    price: float | None
    ocr_confidence: float
    is_noise: bool = False
    match: MatchResult | None = None

    @property
    def needs_review(self) -> bool:
        if self.is_noise or self.match is None or not self.match.is_match:
            return True
        return not self.match.is_auto


def redact_receipt_text(text: str) -> str:
    """Remove card numbers before a receipt line is stored or shown.

    The review has to show the printed words so a person can correct a match.
    It does not have to keep a card number to do that. A shopper's name is left
    alone: guessing which capitalized word is a person eats real product lines.
    """
    text = _CARD_NUMBER.sub("[card]", text)
    return _MASKED_CARD.sub("[card]", text)


def _for_noise_check(text: str) -> str:
    """Undo the OCR confusions that hide an otherwise obvious furniture word.

    ``T0TAL`` and ``GTOTAL`` are the same row as ``TOTAL``, and the noise list
    already knows that word. Replacing every zero would also rewrite prices.
    """
    upper = text.upper()
    upper = re.sub(r"(?<=[A-Z])0(?=[A-Z])", "O", upper)
    return re.sub(r"\bG+TOTAL\b", "TOTAL", upper)


def is_noise_line(text: str) -> bool:
    """True for rows that are receipt furniture rather than purchases."""
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    if _ONLY_NUMERIC.match(stripped):
        return True
    if _DATE_LIKE.search(stripped):
        return True
    if _LONG_DIGITS.search(stripped):
        return True
    if _CARD_NUMBER.search(stripped) or _MASKED_CARD.search(stripped):
        return True
    state_zip = _STATE_ZIP.search(stripped.upper())
    if state_zip and state_zip.group(1) in _STATES:
        return True
    folded = _for_noise_check(stripped)
    if _NOISE.search(stripped) or _NOISE.search(folded):
        return True
    # A product name needs some letters -- but count them across the row rather
    # than demanding a run of three. OCR breaks runs with digit substitutions,
    # so "GV EV00 25.5Z" has no three-letter run yet is a real line item.
    return sum(character.isalpha() for character in stripped) < 3


def extract_quantity(text: str) -> tuple[float | None, str | None]:
    """Pull a purchased amount out of a row.

    Preference order reflects how much each pattern actually tells us: an
    explicit weight beats a multi-buy count, which beats a size printed in the
    description.
    """
    weighed = _WEIGHED.search(text)
    if weighed:
        unit = weighed.group(2).lower()
        return float(weighed.group(1)), _SIZE_UNIT_ALIASES.get(unit, unit)

    multiple = _MULTIPLE.match(text)
    if multiple:
        return float(multiple.group(1)), "count"

    size = _SIZE.search(repair_size_confusions(text))
    if size:
        unit = size.group(2).lower()
        return float(size.group(1)), _SIZE_UNIT_ALIASES.get(unit, unit)

    return None, None


def extract_price(text: str) -> float | None:
    """The last price on a row is the line total; earlier ones are unit prices."""
    matches = _PRICE.findall(text)
    return float(matches[-1]) if matches else None


def parse_line(text: str, confidence: float, matcher: IngredientMatcher) -> ParsedReceiptLine:
    if is_noise_line(text):
        return ParsedReceiptLine(
            raw_text=text, description="", quantity=None, unit=None, price=None,
            ocr_confidence=confidence, is_noise=True,
        )

    quantity, unit = extract_quantity(text)
    price = extract_price(text)
    description = clean_receipt_line(text)

    line = ParsedReceiptLine(
        raw_text=text,
        description=description,
        quantity=quantity,
        unit=unit,
        price=price,
        ocr_confidence=confidence,
    )
    line.match = matcher.match(text, is_receipt=True)
    return line


def merge_continuations(lines: list[OcrLine]) -> list[tuple[str, float]]:
    """Join weight/price rows onto the description row above them.

    Returns ``(text, confidence)`` pairs, taking the lower confidence of any
    merged pair so a shaky continuation doesn't inherit a clean description's
    score.
    """
    merged: list[tuple[str, float]] = []
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if merged and is_continuation(text) and not is_noise_line(merged[-1][0]):
            previous_text, previous_confidence = merged[-1]
            merged[-1] = (f"{previous_text} {text}", min(previous_confidence, line.confidence))
        else:
            merged.append((text, line.confidence))
    return merged


def parse_receipt(result: OcrResult, session: Session) -> list[ParsedReceiptLine]:
    matcher = IngredientMatcher(session)
    return [
        parse_line(text, confidence, matcher)
        for text, confidence in merge_continuations(result.lines)
    ]


def detect_store(lines: list[OcrLine]) -> str | None:
    """Guess the store from the first substantial row, which is the header."""
    from souschef.ingest.ocr import repair_line

    for line in lines[:3]:
        text = line.text.strip()
        if len(text) >= 4 and sum(character.isalpha() for character in text) >= 3:
            name = re.sub(r"[#\d]+\s*$", "", text).strip()
            return repair_line(name).upper() or None
    return None


def detect_total(lines: list[OcrLine]) -> float | None:
    """Find the row labelled TOTAL, preferring it over SUBTOTAL."""
    total: float | None = None
    for line in lines:
        text = line.text
        if re.search(r"\bsub\s*total\b", text, re.IGNORECASE):
            continue
        if re.search(r"\btotal\b", text, re.IGNORECASE):
            price = extract_price(text)
            if price is not None:
                total = price
    return total
