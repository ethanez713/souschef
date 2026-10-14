"""Turn recipe ingredient lines into structured, matched requirements.

Two stages, deliberately separate:

1. ``ingredient-parser-nlp`` splits "2 tablespoons olive oil, divided" into
   quantity, unit, name and comment. It is a CRF model, runs on CPU, and reports
   per-field confidence.
2. Our matcher maps the line to a canonical ingredient.

Keeping them apart matters: the parser is good at grammar and knows nothing
about our catalog, while the matcher knows the catalog and shouldn't be asked to
do grammar. The matcher sees the whole line, because "white or yellow onion"
is one slot and the parser's first name is only half of it. The isolated name
is the fallback when the whole line matches worse.

Vague quantities ("salt to taste", "a drizzle of oil") are flagged rather than
coerced to a number. Inventing 5 g of salt would put noise into the ledger and
eventually onto a shopping list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from souschef.core.matching import IngredientMatcher, MatchResult

# Lines that describe an amount without stating one.
_VAGUE_MARKERS = (
    "to taste", "as needed", "for serving", "for garnish", "a pinch",
    "a dash", "a drizzle", "a splash", "a handful", "some ", "few ",
    "optional", "if desired", "plus more",
)

# A parenthetical "(360g)" or "(200 g)" is the amount the author measured.
# Cups and ounces beside it are the translation, and they disagree with the
# catalog's density often enough to show the wrong number.
_METRIC_UNITS = frozenset({
    "g", "gram", "grams", "kg", "kilogram", "kilograms",
    "ml", "milliliter", "milliliters", "millilitre", "millilitres",
    "l", "liter", "liters", "litre", "litres",
})

# A parenthetical price or "Note 3" is an editor's aside, not part of the food.
# The note form has to go first: "(Note 1)" nested inside another parenthesis
# otherwise ends that parenthesis early, and the "or" beside it is never tried.
_PRICE_ASIDE = re.compile(r"\(\s*\$\s*\d+(?:\.\d+)?\s*\)")
_NOTE_ASIDE = re.compile(
    r"\(\(\s*note\s+\d+\s*\)\)|\(\s*note\s+\d+\s*\)",
    re.IGNORECASE,
)
_TRAILING_OPTIONAL = re.compile(r",\s*optional\s*$", re.IGNORECASE)
_ASIDE_SPACE = re.compile(r"[ \t]{2,}")


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


def _unit_text(raw_unit: object) -> str:
    if raw_unit is None:
        return ""
    if hasattr(raw_unit, "dimensionality"):
        return format(raw_unit, "~")
    return str(raw_unit)


def _is_metric(unit: str) -> bool:
    return unit.strip().lower().rstrip(".") in _METRIC_UNITS


def _is_range(amount: object) -> bool:
    if getattr(amount, "RANGE", False):
        return True
    quantity = _to_float(getattr(amount, "quantity", None))
    quantity_max = _to_float(getattr(amount, "quantity_max", None))
    return quantity is not None and quantity_max is not None and quantity != quantity_max


def _flatten_amounts(amounts: object) -> list[object]:
    flat: list[object] = []
    for amount in amounts or []:
        nested = getattr(amount, "amounts", None)
        if nested:
            flat.extend(_flatten_amounts(nested))
        else:
            flat.append(amount)
    return flat


def _choose_amount(amounts: object) -> tuple[float | None, str | None]:
    """The amount a cook would shop for.

    A stated metric weight wins over the cups it was converted from. A count
    wins over a parenthetical size range ("4 (6- to 8-oz.) breasts" is 4).
    When the only figure is itself a range ("2-3 tsp"), the low end is kept
    rather than dropping the amount.
    """
    parsed: list[tuple[float, str, bool]] = []
    for amount in _flatten_amounts(amounts):
        quantity = _to_float(getattr(amount, "quantity", None))
        if quantity is None:
            continue
        parsed.append((quantity, _unit_text(getattr(amount, "unit", None)), _is_range(amount)))

    metric = [(qty, unit) for qty, unit, is_range in parsed if not is_range and _is_metric(unit)]
    if metric:
        return metric[-1]

    plain = [(qty, unit) for qty, unit, is_range in parsed if not is_range]
    if plain:
        quantity, unit = plain[0]
        return quantity, unit or "count"

    if parsed:
        quantity, unit, _is_range_amount = parsed[0]
        return quantity, unit or "count"
    return None, None


def prepare_for_match(text: str) -> str:
    """Drop prices, note asides and a trailing ", optional" before matching."""
    cleaned = _NOTE_ASIDE.sub(" ", text)
    cleaned = _PRICE_ASIDE.sub(" ", cleaned)
    cleaned = _TRAILING_OPTIONAL.sub("", cleaned)
    return _ASIDE_SPACE.sub(" ", cleaned).strip()


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

    quantity, unit = _choose_amount(parsed.amount)

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
    raw = text.strip()
    prepared = prepare_for_match(raw)
    parsed = parse_line(raw)

    # The whole line carries "or" alternatives the isolated name drops. The
    # matcher tries each side, first one that resolves wins. The parsed name is
    # the fallback when it matches more confidently than the line does.
    result = matcher.match(prepared)
    prepared_name = prepare_for_match(parsed.name) if parsed.name else ""
    if prepared_name and prepared_name != prepared:
        from_name = matcher.match(prepared_name)
        if from_name.confidence > result.confidence:
            result = from_name

    parsed.match = result
    return parsed


def parse_all(lines: list[str], matcher: IngredientMatcher) -> list[ParsedIngredient]:
    return [parse_and_match(line, matcher) for line in lines if line and line.strip()]
