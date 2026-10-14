"""Package nouns derived from Open Food Facts, without calling the network."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_package_units.py"
_spec = importlib.util.spec_from_file_location("fetch_package_units", _SCRIPT)
assert _spec is not None and _spec.loader is not None
off = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(off)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "off" / "black-beans.json"
ON = date(2026, 10, 14)


def _beans() -> dict:
    # Catalog black beans: one count is a 425 g can, and the package is one can.
    return {
        "name": "black beans",
        "category": "legume",
        "base_unit": "count",
        "grams_per_count": 425.0,
        "package_qty": 1,
        "package_unit": "count",
    }


def test_black_beans_fixture_aggregates_to_one_can() -> None:
    products = json.loads(FIXTURE.read_text(encoding="utf-8"))["products"]
    row = off.aggregate(products, _beans(), on=ON)

    # Three cans (food-can, can, can). The "canned" tag, the 130 g cup and the
    # boxed bowl are real products in the fixture and do not enter the median:
    # including every sized product would land on 0.97 count instead of 1.
    assert row["package_noun"] == "can"
    assert row["agreeing"] == 3
    assert row["product_count"] == 3
    assert row["median_base_qty"] == 1
    assert row["base_unit"] == "count"
    assert row["package_source"] == "Open Food Facts, median of 3 US products, 2026-10"


def test_fresh_produce_gets_no_package_noun() -> None:
    products = json.loads(FIXTURE.read_text(encoding="utf-8"))["products"]
    onion = {"name": "onion", "category": "produce", "base_unit": "count", "grams_per_count": 150}
    row = off.aggregate(products, onion, on=ON)
    assert row["package_noun"] is None
    assert row["median_base_qty"] is None
    assert row["package_source"] is None
    assert row["agreeing"] == 0


@pytest.mark.parametrize(
    ("tag", "word"),
    [
        ("en:can", "can"),
        ("en:tin", "can"),
        ("en:food-can", "can"),
        ("en:aluminium-tin", "can"),
        ("en:bag", "bag"),
        ("en:pouch", "bag"),
        ("en:stand-up-pouch", "bag"),
        ("en:sachet", "pack"),
        ("en:packet", "pack"),
        ("en:jar", "jar"),
        ("en:bottle", "bottle"),
        ("en:box", "box"),
        ("en:carton", "carton"),
        ("en:tub", "tub"),
        ("en:loaf", "loaf"),
        ("en:bunch", "bunch"),
        ("en:canned", None),
        ("en:bowl", None),
    ],
)
def test_off_tag_maps_onto_the_container_list(tag: str, word: str | None) -> None:
    assert off.container_of({"packaging_shapes_tags": [tag]}) == word


def test_unmapped_shapes_are_skipped_until_a_container() -> None:
    product = {"packaging_shapes_tags": ["en:bowl", "en:box", "en:seal"]}
    assert off.container_of(product) == "box"
    shapes_win = {"packaging_shapes_tags": ["en:can"], "packaging_tags": ["en:box"]}
    assert off.container_of(shapes_win) == "can"


def test_ounces_convert_to_the_ingredient_base_unit() -> None:
    products = [{
        "product_quantity": "15 oz",
        "product_quantity_unit": "",
        "packaging_shapes_tags": ["en:can"],
    }]
    ingredient = {"name": "tomato paste", "category": "canned", "base_unit": "g"}
    row = off.aggregate(products, ingredient, on=ON)
    assert row["package_noun"] == "can"
    assert row["median_base_qty"] == 425
    assert row["product_count"] == 1


def test_votes_without_a_size_still_count_toward_agreement() -> None:
    products = [
        {
            "product_quantity": 400,
            "product_quantity_unit": "g",
            "packaging_shapes_tags": ["en:bag"],
        },
        {
            "product_quantity": 500,
            "product_quantity_unit": "g",
            "packaging_shapes_tags": ["en:bag"],
        },
        {"packaging_shapes_tags": ["en:bag"]},
        {"packaging_shapes_tags": ["en:bag"]},
        {"packaging_shapes_tags": ["en:bag"]},
    ]
    ingredient = {"name": "rice", "category": "grain", "base_unit": "g", "package_qty": 454}
    row = off.aggregate(products, ingredient, on=ON)
    assert row["agreeing"] == 5
    assert row["product_count"] == 2
    assert row["median_base_qty"] == 450
    assert row["package_source"] == "Open Food Facts, median of 2 US products, 2026-10"


def _review(
    noun: str = "can",
    median: float = 1,
    agreeing: int = 5,
    source: str | None = None,
) -> dict:
    return {
        "name": "black beans",
        "package_noun": noun,
        "median_base_qty": median,
        "base_unit": "count",
        "product_count": agreeing,
        "agreeing": agreeing,
        "package_source": (
            source if source is not None
            else f"Open Food Facts, median of {agreeing} US products, 2026-10"
        ),
    }


@pytest.mark.parametrize(
    ("median", "agreeing", "noun", "category", "ok"),
    [
        (1, 5, "can", "legume", True),
        (1, 4, "can", "legume", False),
        (4, 5, "can", "legume", True),
        (4.01, 5, "can", "legume", False),
        (0.25, 5, "can", "legume", True),
        (0.24, 5, "can", "legume", False),
        (1, 8, "bag", "produce", False),
        (1, 5, "bucket", "legume", False),
    ],
)
def test_apply_gates(median: float, agreeing: int, noun: str, category: str, ok: bool) -> None:
    ingredient = _beans()
    ingredient["category"] = category
    assert off.accepted(_review(noun, median, agreeing), ingredient) is ok


def test_apply_merges_only_rows_that_clear_the_gates() -> None:
    beans = _beans()
    beans["aliases"] = ["blk beans"]
    onion = {"name": "onion", "category": "produce", "base_unit": "count", "package_qty": 1,
             "package_unit": "count"}
    flour = {"name": "all-purpose flour", "category": "baking", "base_unit": "g",
             "package_qty": 2270, "package_unit": "g"}
    catalog = {"_comment": "keep", "ingredients": [beans, onion, flour]}
    review = {"ingredients": [
        _review(),
        {**_review(noun="bag", median=1, agreeing=8), "name": "onion", "base_unit": "count"},
        {
            "name": "all-purpose flour",
            "package_noun": "bag",
            "median_base_qty": 20000,
            "base_unit": "g",
            "agreeing": 9,
            "package_source": "Open Food Facts, median of 9 US products, 2026-10",
        },
    ]}
    assert off.apply_review(catalog, review) == 1
    assert beans["package_noun"] == "can"
    assert beans["package_source"] == "Open Food Facts, median of 5 US products, 2026-10"
    assert beans["package_qty"] == 1
    assert beans["aliases"] == ["blk beans"]
    assert "package_noun" not in onion
    assert "package_noun" not in flour
    assert flour["package_qty"] == 2270
    assert catalog["_comment"] == "keep"


def test_seed_reads_package_noun_and_source(session, tmp_path: Path) -> None:
    from souschef.core.seeding import seed_ingredients
    from souschef.models import CanonicalIngredient

    seed_path = tmp_path / "ingredients.json"
    seed_path.write_text(json.dumps({"ingredients": [
        {
            "name": "black beans",
            "category": "legume",
            "base_unit": "count",
            "package_noun": "can",
            "package_source": "Open Food Facts, median of 8 US products, 2026-10",
        },
        {"name": "onion", "category": "produce", "base_unit": "count"},
    ]}), encoding="utf-8")
    seed_ingredients(session, seed_path)
    rows = {ing.name: ing for ing in session.scalars(select(CanonicalIngredient))}
    assert rows["black beans"].package_noun == "can"
    assert rows["black beans"].package_source == "Open Food Facts, median of 8 US products, 2026-10"
    assert rows["onion"].package_noun is None
    assert rows["onion"].package_source is None


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_fetch_skips_produce_and_paces_requests(tmp_path: Path, monkeypatch) -> None:
    ingredients = tmp_path / "ingredients.json"
    review = tmp_path / "package_units.review.json"
    ingredients.write_text(json.dumps({"ingredients": [
        {"name": "black beans", "category": "legume", "base_unit": "g",
         "package_qty": 425, "package_unit": "g"},
        {"name": "onion", "category": "produce", "base_unit": "count"},
        {"name": "chickpeas", "category": "legume", "base_unit": "g",
         "package_qty": 425, "package_unit": "g"},
    ]}), encoding="utf-8")
    before = ingredients.read_text(encoding="utf-8")
    monkeypatch.setattr(off, "INGREDIENTS_PATH", ingredients)
    monkeypatch.setattr(off, "REVIEW_PATH", review)
    seen: list[tuple[str, str]] = []

    def opener(request, timeout):
        assert timeout == 60
        seen.append((request.full_url, request.get_header("User-agent")))
        body = {
            "products": [{
                "product_quantity": 400,
                "product_quantity_unit": "g",
                "packaging_shapes_tags": ["en:can"],
            }]
        }
        return _Body(json.dumps(body).encode())

    slept: list[float] = []
    monkeypatch.setattr(off.urllib.request, "urlopen", opener)
    monkeypatch.setattr(off.time, "sleep", slept.append)
    assert off.main(["--only", "black beans,onion,chickpeas"]) == 0
    assert ingredients.read_text(encoding="utf-8") == before
    assert [url for url, _ua in seen] == [
        off.search_url("black beans"),
        off.search_url("chickpeas"),
    ]
    assert {ua for _url, ua in seen} == {off.USER_AGENT}
    assert slept == [pytest.approx(off.REQUEST_INTERVAL_S, abs=0.2)]
    saved = json.loads(review.read_text(encoding="utf-8"))
    assert [row["name"] for row in saved["ingredients"]] == ["black beans", "chickpeas"]
    assert saved["ingredients"][0]["package_noun"] == "can"
    assert "onion" not in review.read_text(encoding="utf-8")


def test_unknown_only_name_fetches_nothing(tmp_path: Path, monkeypatch) -> None:
    ingredients = tmp_path / "ingredients.json"
    ingredients.write_text(json.dumps({"ingredients": [
        {"name": "black beans", "category": "legume", "base_unit": "g"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(off, "INGREDIENTS_PATH", ingredients)
    monkeypatch.setattr(off, "REVIEW_PATH", tmp_path / "review.json")
    assert off.main(["--only", "diced tomatoes"]) == 1
    assert not (tmp_path / "review.json").exists()


def test_apply_command_needs_a_review_file(tmp_path: Path, monkeypatch) -> None:
    ingredients = tmp_path / "ingredients.json"
    ingredients.write_text(json.dumps({"ingredients": []}), encoding="utf-8")
    monkeypatch.setattr(off, "INGREDIENTS_PATH", ingredients)
    monkeypatch.setattr(off, "REVIEW_PATH", tmp_path / "missing.json")
    assert off.main(["--apply"]) == 1
    assert ingredients.read_text(encoding="utf-8") == json.dumps({"ingredients": []})


def test_search_url_is_the_us_product_search() -> None:
    parsed = off.urllib.parse.urlsplit(off.search_url("black beans"))
    query = off.urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "world.openfoodfacts.org"
    assert parsed.path == "/cgi/search.pl"
    assert query["json"] == ["1"]
    assert query["search_terms"] == ["black beans"]
    assert query["tagtype_0"] == ["countries"]
    assert query["tag_contains_0"] == ["contains"]
    assert query["tag_0"] == ["united-states"]
    assert query["page_size"] == ["50"]
    assert query["fields"] == [
        "product_quantity,product_quantity_unit,packaging_shapes_tags,packaging_tags"
    ]
    assert off.USER_AGENT == "SousChef/0.1 (personal pantry app)"
    assert off.REQUESTS_PER_MINUTE == 10
