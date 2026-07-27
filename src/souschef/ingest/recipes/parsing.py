"""Turn recipe ingredient lines into structured, matched requirements.

Two stages, deliberately separate:

1. ``ingredient-parser-nlp`` splits "2 tablespoons olive oil, divided" into
   quantity, unit, name and comment. It is a CRF model, runs on CPU, and reports
   per-field confidence.
2. Our matcher maps the extracted *name* to a canonical ingredient.

Keeping them apart matters: the parser is good at grammar and knows nothing
about our catalog, while the matcher knows the catalog and shouldn't be asked to
do grammar. Feeding the matcher the parser's isolated name field, rather than
the whole line, is what makes matching accurate.

Vague quantities ("salt to taste", "a drizzle of oil") are flagged rather than
coerced to a number. Inventing 5 g of salt would put noise into the ledger and
eventually onto a shopping list.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from souschef.core.matching import IngredientMatcher, MatchResult

# Lines that describe an amount without stating one.
_VAGUE_MARKERS = (
    "to taste", "as needed", "for serving", "for garnish", "a pinch",
    "a dash", "a drizzle", "a splash", "a handful", "some ", "few ",
    "optional", "if desired", "plus more",
)


@dataclass
class ParsedIngredient:
    raw_text: str
    name: str
    quantity: float | None
    unit: str | None
    preparation: str | None
    is_optional: bool
    quantity_is_vague: bool
    parse_confidence: float
    match: MatchResult | None = None

    @property
    def needs_review(self) -> bool:
        """True when a human should look before this is trusted."""
        if self.match is None or not self.match.is_match:
            return True
        return not self.match.is_auto


def _is_vague(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _VAGUE_MARKERS)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_line(text: str) -> ParsedIngredient:
    """Parse one ingredient line into structured fields.

    Falls back to treating the whole line as a name if the parser errors -- a
    bad parse should degrade to "needs review", never crash an import.
    """
    from ingredient_parser import parse_ingredient

    raw = text.strip()
    vague = _is_vague(raw)

    try:
        parsed = parse_ingredient(raw)
    except Exception:  # noqa: BLE001 - any parser failure degrades to manual review
        return ParsedIngredient(
            raw_text=raw, name=raw, quantity=None, unit=None, preparation=None,
            is_optional=vague, quantity_is_vague=True, parse_confidence=0.0,
        )

    name = ""
    name_conf = 0.0
    if parsed.name:
        first = parsed.name[0]
        name = first.text
        name_conf = float(getattr(first, "confidence", 0.0) or 0.0)

    quantity: float | None = None
    unit: str | None = None
    if parsed.amount:
        amount = parsed.amount[0]
        quantity = _to_float(getattr(amount, "quantity", None))
        raw_unit = getattr(amount, "unit", None)
        if raw_unit is not None:
            # ingredient-parser returns pint Unit objects where it can, and
            # plain strings otherwise.
            unit = str(getattr(raw_unit, "_units", None) or raw_unit)
            unit = format(raw_unit, "~") if hasattr(raw_unit, "dimensionality") else str(raw_unit)

    preparation = None
    if getattr(parsed, "preparation", None):
        preparation = str(parsed.preparation.text)

    comment_text = ""
    if getattr(parsed, "comment", None):
        comment_text = str(parsed.comment.text).lower()

    return ParsedIngredient(
        raw_text=raw,
        name=name or raw,
        quantity=quantity,
        unit=unit,
        preparation=preparation,
        is_optional=vague or "optional" in comment_text,
        quantity_is_vague=vague or quantity is None,
        parse_confidence=name_conf,
    )


def parse_and_match(text: str, matcher: IngredientMatcher) -> ParsedIngredient:
    """Parse a line and resolve it to a canonical ingredient."""
    parsed = parse_line(text)

    # Match on the isolated name first; it carries the least noise. Fall back to
    # the full line, since a failed name extraction shouldn't lose the match.
    result = matcher.match(parsed.name)
    if not result.is_auto:
        from_full = matcher.match(text)
        if from_full.confidence > result.confidence:
            result = from_full

    parsed.match = result
    return parsed


def parse_all(lines: list[str], matcher: IngredientMatcher) -> list[ParsedIngredient]:
    return [parse_and_match(line, matcher) for line in lines if line and line.strip()]
