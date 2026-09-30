"""Unit conversion tests.

Expected values are real-world kitchen figures (a cup of AP flour is ~125 g, a
tablespoon of olive oil ~13.6 g), so a regression here shows up as a number a
cook would recognise as wrong.
"""

from __future__ import annotations

import pytest

from souschef.core.units import (
    Dimension,
    IngredientPhysical,
    UnitConversionError,
    convert,
    count_factors_for,
    dimension_of,
    humanize,
    normalize_unit,
    to_base,
    to_grams,
)

FLOUR = IngredientPhysical(density_g_per_ml=0.529, base_unit="g")
OIL = IngredientPhysical(density_g_per_ml=0.918, base_unit="ml")
EGG = IngredientPhysical(grams_per_count=50.0, base_unit="count")
UNKNOWN = IngredientPhysical()


@pytest.mark.parametrize(
    ("qty", "unit", "physical", "expected"),
    [
        (2, "cups", FLOUR, 250.3),  # ~125 g/cup
        (1, "cup", FLOUR, 125.1),
        (1, "tbsp", OIL, 13.6),
        (5, "lb", FLOUR, 2268.0),
        (3, "count", EGG, 150.0),
        (500, "g", FLOUR, 500.0),
        (1, "kg", FLOUR, 1000.0),
    ],
)
def test_to_grams(qty: float, unit: str, physical: IngredientPhysical, expected: float) -> None:
    assert to_grams(qty, unit, physical) == pytest.approx(expected, rel=0.01)


def test_volume_mass_roundtrip_is_lossless() -> None:
    grams = to_grams(2, "cups", FLOUR)
    assert convert(grams, "g", "cups", FLOUR) == pytest.approx(2.0, rel=1e-6)


def test_count_mass_roundtrip_is_lossless() -> None:
    grams = to_grams(6, "count", EGG)
    assert convert(grams, "g", "count", EGG) == pytest.approx(6.0, rel=1e-6)


def test_same_dimension_needs_no_ingredient_facts() -> None:
    assert convert(1000, "g", "kg") == pytest.approx(1.0)
    assert convert(1, "liter", "ml") == pytest.approx(1000.0)
    assert convert(16, "oz", "lb") == pytest.approx(1.0)


def test_crossing_dimensions_without_facts_raises_rather_than_guessing() -> None:
    """Refusing to guess is the point: a wrong number silently corrupts the pantry."""
    with pytest.raises(UnitConversionError):
        to_grams(1, "cup", UNKNOWN)
    with pytest.raises(UnitConversionError):
        to_grams(1, "count", UNKNOWN)
    with pytest.raises(UnitConversionError):
        convert(100, "g", "cups", UNKNOWN)


def test_to_base_uses_the_ingredients_own_unit() -> None:
    assert to_base(2, "tbsp", OIL) == pytest.approx(29.57, rel=0.01)  # oil is ml-based
    assert to_base(1, "cup", FLOUR) == pytest.approx(125.1, rel=0.01)  # flour is g-based
    assert to_base(2, "count", EGG) == pytest.approx(2.0)  # eggs are count-based


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("tbsp", "tablespoon"), ("Tbsp.", "tablespoon"), ("TBSP", "tablespoon"),
        ("c", "cup"), ("cups", "cup"), ("lbs", "pound"), ("#", "pound"),
        ("fl oz", "fluid_ounce"), ("", "count"), (None, "count"),
    ],
)
def test_unit_aliases(unit: str | None, expected: str) -> None:
    assert normalize_unit(unit) == expected


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("g", Dimension.MASS), ("kg", Dimension.MASS), ("lb", Dimension.MASS),
        ("ml", Dimension.VOLUME), ("cup", Dimension.VOLUME), ("tbsp", Dimension.VOLUME),
        ("count", Dimension.COUNT), ("clove", Dimension.COUNT), ("can", Dimension.COUNT),
    ],
)
def test_dimension_classification(unit: str, expected: str) -> None:
    assert dimension_of(unit) == expected


def test_unknown_unit_raises() -> None:
    with pytest.raises(UnitConversionError):
        dimension_of("furlong-per-fortnight")


def test_bag_is_not_converted_as_a_sack_of_cement() -> None:
    """pint's bag is 94 lb. A bag of spinach is not that."""
    with pytest.raises(UnitConversionError, match="bag"):
        to_base(1, "bag", IngredientPhysical(base_unit="g", density_g_per_ml=0.2))


def test_a_head_of_garlic_is_ten_cloves() -> None:
    assert count_factors_for("garlic")["head"] == 10
    assert count_factors_for("spinach") == {}
    physical = IngredientPhysical(
        base_unit="count", grams_per_count=3.0, count_factors={"head": 10.0}
    )
    assert to_base(1, "head", physical) == 10
    assert to_base(2, "clove", physical) == 2


@pytest.mark.parametrize(
    ("qty", "unit", "expected"),
    [
        (1500, "g", "1.50 kg"), (250, "g", "250 g"), (12.5, "g", "12.5 g"),
        (3, "count", "3"), (1, "count", "1"),
        (2000, "ml", "2 L"),  # whole numbers drop the decimals
        (2500, "ml", "2.50 L"),
    ],
)
def test_humanize(qty: float, unit: str, expected: str) -> None:
    assert humanize(qty, unit) == expected
