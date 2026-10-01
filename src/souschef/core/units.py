"""Unit handling and the mass/volume/count bridge.

The central problem: recipes speak in volume ("2 cups flour"), shops sell by
mass ("5 LB FLOUR"), and some things are counted ("3 eggs"). These three
vocabularies have to reconcile or nothing downstream works -- you cannot
subtract a recipe from a pantry stocked by receipts otherwise.

Bridging them needs per-ingredient physical facts:
  * volume -> mass needs density (a cup of flour is 120 g; a cup of oil is 216 g)
  * count  -> mass needs a per-item weight (an egg is ~50 g)

Both live on ``CanonicalIngredient``. When one is missing we refuse to guess and
raise ``UnitConversionError``; callers degrade to coarse level tracking rather
than silently inventing a number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pint

# A single shared registry. Building one is expensive and pint quantities from
# different registries cannot be compared, so everything must use this instance.
_ureg = pint.UnitRegistry()

# pint ships a dimensionless `count`; we need it to carry its own dimension so
# "3 eggs" cannot be silently added to "3 grams". The override is deliberate, so
# quiet the redefinition warning -- an unactionable WARNING on every startup
# just teaches you to ignore the log.
_pint_log_level = logging.getLogger("pint.util").level
logging.getLogger("pint.util").setLevel(logging.ERROR)
_ureg.define("count = [item]")
logging.getLogger("pint.util").setLevel(_pint_log_level)

_ureg.define("piece = 1 * count")
_ureg.define("clove = 1 * count")
_ureg.define("slice = 1 * count")
_ureg.define("can = 1 * count")
_ureg.define("package = 1 * count")
_ureg.define("bunch = 1 * count")
_ureg.define("head = 1 * count")
_ureg.define("stalk = 1 * count")
_ureg.define("sprig = 1 * count")
_ureg.define("pinch = 0.31 * milliliter")  # ~1/16 tsp
_ureg.define("dash = 0.62 * milliliter")

BASE_MASS = "g"
BASE_VOLUME = "ml"
BASE_COUNT = "count"

# Free-text unit spellings seen in recipes and on receipts, mapped to pint names.
_UNIT_ALIASES = {
    "": BASE_COUNT,
    "ct": BASE_COUNT,
    "each": BASE_COUNT,
    "ea": BASE_COUNT,
    "pc": "piece",
    "pcs": "piece",
    "pkg": "package",
    "pk": "package",
    "t": "teaspoon",
    "tsp": "teaspoon",
    "tsps": "teaspoon",
    "teaspoons": "teaspoon",
    "tb": "tablespoon",
    "tbs": "tablespoon",
    "tbsp": "tablespoon",
    "tbsps": "tablespoon",
    "tablespoons": "tablespoon",
    "c": "cup",
    "cp": "cup",  # pint's short format for cup
    "cups": "cup",
    "fl oz": "fluid_ounce",
    "floz": "fluid_ounce",
    "fluid ounce": "fluid_ounce",
    "fluid ounces": "fluid_ounce",
    "oz": "ounce",
    "ozs": "ounce",
    "ounces": "ounce",
    "lb": "pound",
    "lbs": "pound",
    "pound": "pound",
    "pounds": "pound",
    "#": "pound",
    "g": "gram",
    "gr": "gram",
    "gram": "gram",
    "grams": "gram",
    "kg": "kilogram",
    "kgs": "kilogram",
    "kilogram": "kilogram",
    "kilograms": "kilogram",
    "ml": "milliliter",
    "milliliters": "milliliter",
    "l": "liter",
    "lt": "liter",
    "liters": "liter",
    "litres": "liter",
    "qt": "quart",
    "quarts": "quart",
    "pt": "pint",
    "pints": "pint",
    "gal": "gallon",
    "gallons": "gallon",
    "cloves": "clove",
    "slices": "slice",
    "cans": "can",
    "packages": "package",
    "bunches": "bunch",
    "heads": "head",
    "stalks": "stalk",
    "sprigs": "sprig",
    "pieces": "piece",
}


class UnitConversionError(ValueError):
    """A conversion was requested that cannot be done without guessing."""


class Dimension(str):
    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


@dataclass(frozen=True)
class IngredientPhysical:
    """The per-ingredient facts needed to cross dimensions.

    Deliberately decoupled from the ORM model so the conversion layer stays
    pure and trivially testable.
    """

    density_g_per_ml: float | None = None
    grams_per_count: float | None = None
    base_unit: str = BASE_MASS
    # Named count-units that are not one of the ingredient's base counts.
    # "head" of garlic is many cloves; "head" of lettuce is already one count.
    count_factors: dict[str, float] | None = None


# pint's ``bag`` is a 94 lb sack of cement. Kitchen speech ("a bag of spinach")
# is not that unit, and guessing a weight here has stored 40 kg of leaves.
_NOT_A_KITCHEN_UNIT = frozenset({
    "bag", "bags", "box", "boxes", "jar", "jars", "carton", "cartons",
    "bottle", "bottles", "pack", "packs", "packet", "packets",
})

# Garlic is counted in cloves (its grams_per_count is one clove). A typical
# head is about 10 of those. Ingredients absent from this map keep every named
# count-unit as one count, which is right for "1 head" of lettuce.
_COUNT_FACTORS: dict[str, dict[str, float]] = {
    "garlic": {"head": 10.0, "bulb": 10.0},
}


def count_factors_for(name: str) -> dict[str, float]:
    return dict(_COUNT_FACTORS.get(name, ()))


def normalize_unit(unit: str | None) -> str:
    """Map a free-text unit onto a pint-parseable name."""
    if unit is None:
        return BASE_COUNT
    cleaned = str(unit).strip().lower().rstrip(".")
    if cleaned in _UNIT_ALIASES:
        return _UNIT_ALIASES[cleaned]
    return cleaned or BASE_COUNT


def _count_scale(unit: str, physical: IngredientPhysical) -> float:
    factors = physical.count_factors or {}
    return float(factors.get(normalize_unit(unit), 1.0))


def dimension_of(unit: str) -> str:
    """Classify a unit as mass, volume, or count."""
    normalized = normalize_unit(unit)
    if normalized in _NOT_A_KITCHEN_UNIT:
        raise UnitConversionError(
            f"Don't know how much one {normalized} weighs. "
            "Enter grams, ounces, pounds, or a count."
        )
    if normalized in (BASE_COUNT, "piece", "clove", "slice", "can", "package",
                      "bunch", "head", "stalk", "sprig"):
        return Dimension.COUNT
    try:
        q = _ureg.Quantity(1, normalized)
    except (pint.UndefinedUnitError, TypeError) as exc:
        raise UnitConversionError(f"Unknown unit {unit!r}") from exc
    if q.check("[mass]"):
        return Dimension.MASS
    if q.check("[volume]"):
        return Dimension.VOLUME
    if q.check("[item]"):
        return Dimension.COUNT
    raise UnitConversionError(f"Unit {unit!r} is neither mass, volume, nor count")


def to_grams(quantity: float, unit: str, physical: IngredientPhysical) -> float:
    """Convert any quantity to grams, crossing dimensions when facts allow."""
    normalized = normalize_unit(unit)
    dim = dimension_of(normalized)

    if dim == Dimension.MASS:
        return float(_ureg.Quantity(quantity, normalized).to("gram").magnitude)

    if dim == Dimension.VOLUME:
        if physical.density_g_per_ml is None:
            raise UnitConversionError(
                f"Cannot convert {quantity} {unit} to mass without a density"
            )
        ml = float(_ureg.Quantity(quantity, normalized).to("milliliter").magnitude)
        return ml * physical.density_g_per_ml

    if physical.grams_per_count is None:
        raise UnitConversionError(
            f"Cannot convert {quantity} {unit} to mass without a per-item weight"
        )
    return quantity * _count_scale(normalized, physical) * physical.grams_per_count


def convert(
    quantity: float,
    from_unit: str,
    to_unit: str,
    physical: IngredientPhysical | None = None,
) -> float:
    """Convert between arbitrary units, using ingredient facts to cross dimensions."""
    physical = physical or IngredientPhysical()
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)

    if src == dst:
        return float(quantity)

    src_dim, dst_dim = dimension_of(src), dimension_of(dst)

    # Same dimension: pure pint, no ingredient facts needed.
    if src_dim == dst_dim:
        if src_dim == Dimension.COUNT:
            scale = _count_scale(src, physical) / _count_scale(dst, physical)
            return float(quantity) * scale
        return float(_ureg.Quantity(quantity, src).to(dst).magnitude)

    # Cross-dimension: route through grams.
    grams = to_grams(quantity, src, physical)

    if dst_dim == Dimension.MASS:
        return float(_ureg.Quantity(grams, "gram").to(dst).magnitude)

    if dst_dim == Dimension.VOLUME:
        if physical.density_g_per_ml is None:
            raise UnitConversionError(f"Cannot convert to {to_unit} without a density")
        ml = grams / physical.density_g_per_ml
        return float(_ureg.Quantity(ml, "milliliter").to(dst).magnitude)

    if physical.grams_per_count is None:
        raise UnitConversionError(f"Cannot convert to {to_unit} without a per-item weight")
    return grams / physical.grams_per_count


def to_base(quantity: float, unit: str | None, physical: IngredientPhysical) -> float:
    """Convert into the ingredient's own base unit -- how pantry levels are stored."""
    return convert(quantity, normalize_unit(unit), physical.base_unit, physical)


_GRAMS_PER_OUNCE = 28.3495
_ML_PER_FLUID_OUNCE = 29.5735
_OUNCES_PER_POUND = 16


def _us_number(value: float) -> str:
    return f"{value:.0f}" if value >= 100 else f"{value:.1f}".removesuffix(".0")


def us_equivalent(quantity: float, unit: str) -> str | None:
    """Ounces, pounds or fluid ounces for a metric weight or volume; None otherwise.

    American packages are labelled in these, so a list line shows both.
    """
    normalized = normalize_unit(unit)
    if normalized in ("kilogram", "liter"):
        quantity, normalized = quantity * 1000, "gram" if normalized == "kilogram" else "milliliter"
    if normalized == "gram":
        ounces = quantity / _GRAMS_PER_OUNCE
        if ounces >= 2 * _OUNCES_PER_POUND:
            return f"{_us_number(ounces / _OUNCES_PER_POUND)} lb"
        return f"{_us_number(ounces)} oz"
    if normalized == "milliliter":
        return f"{_us_number(quantity / _ML_PER_FLUID_OUNCE)} fl oz"
    return None


def humanize(quantity: float, unit: str, *, us: bool = False) -> str:
    """Render a quantity the way a person would write it on a list.

    With ``us``, a metric weight or volume also carries its ounce equivalent.
    """
    text = _humanize(quantity, unit)
    equivalent = us_equivalent(quantity, unit) if us else None
    return f"{text} ({equivalent})" if equivalent else text


def _humanize(quantity: float, unit: str) -> str:
    normalized = normalize_unit(unit)

    if normalized == BASE_COUNT or dimension_of(normalized) == Dimension.COUNT:
        rounded = round(quantity)
        if abs(quantity - rounded) < 0.05:
            return str(int(rounded))
        return f"{quantity:.1f}"

    # Promote to the larger unit once the number gets unwieldy.
    if normalized == "gram" and quantity >= 1000:
        return f"{quantity / 1000:.2f} kg".replace(".00 ", " ")
    if normalized == "milliliter" and quantity >= 1000:
        return f"{quantity / 1000:.2f} L".replace(".00 ", " ")

    display = {"gram": "g", "milliliter": "ml", "kilogram": "kg", "liter": "L",
               "ounce": "oz", "pound": "lb", "fluid_ounce": "fl oz"}.get(normalized, normalized)

    if quantity >= 100:
        return f"{quantity:.0f} {display}"
    if abs(quantity - round(quantity)) < 0.05:
        return f"{int(round(quantity))} {display}"
    return f"{quantity:.1f} {display}"
