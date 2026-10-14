"""Derive catalog package units from US Open Food Facts products.

The default run fetches and writes data/seeds/package_units.review.json.
``--apply`` merges reviewed rows into data/seeds/ingredients.json when at least
5 products share a container and the median size is within 4× the catalog
package. Fresh produce is skipped: its count noun leads instead.

Parsing and aggregation are pure. Nothing here runs from the app or the tests.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

# The script lives outside the package; running it as a file still finds src.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from souschef.core.units import IngredientPhysical, UnitConversionError, to_base  # noqa: E402

# The plan caps this search at 10 requests a minute.
REQUESTS_PER_MINUTE = 10
REQUEST_INTERVAL_S = 60 / REQUESTS_PER_MINUTE
USER_AGENT = "SousChef/0.1 (personal pantry app)"

# Apply only when at least this many products share the winning container and
# the median is within this factor of the catalog package size.
AGREE_MIN = 5
SIZE_RATIO = 4
PRODUCE_CATEGORY = "produce"

# Singular container words the add form knows.
PACKAGE_NOUNS = frozenset({
    "can", "bag", "jar", "bottle", "box", "carton", "pack", "tub", "loaf", "bunch",
})

# OFF tag, after the language prefix, to one of those words. "canned" is absent
# on purpose: it names a process, and a canned food can still be a jar.
_TAG_WORDS = {
    "can": "can",
    "tin": "can",
    "bag": "bag",
    "jar": "jar",
    "bottle": "bottle",
    "box": "box",
    "carton": "carton",
    "tub": "tub",
    "pouch": "bag",
    "sachet": "pack",
    "pack": "pack",
    "packet": "pack",
    "loaf": "loaf",
    "bunch": "bunch",
}

ROOT = Path(__file__).resolve().parents[1]
INGREDIENTS_PATH = ROOT / "data" / "seeds" / "ingredients.json"
REVIEW_PATH = ROOT / "data" / "seeds" / "package_units.review.json"

_SEARCH = "https://world.openfoodfacts.org/cgi/search.pl"


def search_url(name: str) -> str:
    query = urllib.parse.urlencode({
        "json": "1",
        "search_terms": name,
        "tagtype_0": "countries",
        "tag_contains_0": "contains",
        "tag_0": "united-states",
        "page_size": "50",
        "fields": (
            "product_quantity,product_quantity_unit,packaging_shapes_tags,packaging_tags"
        ),
    })
    return f"{_SEARCH}?{query}"


def _word_from_tag(tag: str) -> str | None:
    local = tag.strip().lower().split(":")[-1].replace("_", "-")
    if local in _TAG_WORDS:
        return _TAG_WORDS[local]
    # "food-can", "aluminium-tin", "stand-up-pouch": the container is the last hit.
    for part in reversed(local.split("-")):
        word = _TAG_WORDS.get(part)
        if word:
            return word
    return None


def container_of(product: dict) -> str | None:
    """The plan's container word for one product, or None when the tags don't say."""
    for key in ("packaging_shapes_tags", "packaging_tags"):
        tags = product.get(key)
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if isinstance(tag, str):
                word = _word_from_tag(tag)
                if word:
                    return word
    return None


def parse_quantity(product: dict) -> tuple[float, str] | None:
    """``(amount, unit)`` from an OFF product, or None when either is missing."""
    raw = product.get("product_quantity")
    unit = product.get("product_quantity_unit")
    unit_text = unit.strip().lower() if isinstance(unit, str) else ""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        quantity = float(raw)
    elif isinstance(raw, str):
        text = raw.strip().lower().replace(",", "")
        if not text:
            return None
        head, _, rest = text.partition(" ")
        try:
            quantity = float(head)
        except ValueError:
            return None
        if rest and not unit_text:
            unit_text = rest.strip()
    else:
        return None
    if quantity <= 0 or not unit_text:
        return None
    return quantity, unit_text


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def size_in_base(quantity: float, unit: str, ingredient: dict) -> float | None:
    """Convert one OFF quantity into the ingredient's base unit, or None."""
    physical = IngredientPhysical(
        density_g_per_ml=_optional_float(ingredient.get("density_g_per_ml")),
        grams_per_count=_optional_float(ingredient.get("grams_per_count")),
        base_unit=str(ingredient.get("base_unit") or "g"),
    )
    try:
        size = to_base(quantity, unit, physical)
    except (UnitConversionError, TypeError, ValueError):
        # pint's DimensionalityError is a TypeError; an unknown unit is a ValueError.
        return None
    if size <= 0:
        return None
    return float(size)


def _round_base(value: float, base_unit: str) -> int | float:
    rounded = round(float(value), 2) if base_unit == "count" else round(float(value))
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def _winner(votes: Counter[str]) -> tuple[str | None, int]:
    if not votes:
        return None, 0
    ranked = votes.most_common()
    word, count = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == count:
        return None, 0
    return word, count


def aggregate(products: list, ingredient: dict, *, on: date) -> dict:
    """Most common container, median size in the base unit, and the provenance line.

    The median uses only products that carry the winning container and a size
    that converts. A tie, or fresh produce, leaves the noun empty.
    """
    base = str(ingredient.get("base_unit") or "g")
    row = {
        "name": ingredient.get("name"),
        "package_noun": None,
        "median_base_qty": None,
        "base_unit": base,
        "product_count": 0,
        "agreeing": 0,
        "package_source": None,
    }
    if ingredient.get("category") == PRODUCE_CATEGORY:
        return row

    votes: Counter[str] = Counter()
    sizes: dict[str, list[float]] = {}
    for product in products:
        if not isinstance(product, dict):
            continue
        word = container_of(product)
        if not word:
            continue
        votes[word] += 1
        parsed = parse_quantity(product)
        if parsed is None:
            continue
        size = size_in_base(parsed[0], parsed[1], ingredient)
        if size is None:
            continue
        sizes.setdefault(word, []).append(size)

    noun, agreeing = _winner(votes)
    row["package_noun"] = noun
    row["agreeing"] = agreeing
    sample = sizes.get(noun, []) if noun else []
    if not sample:
        return row
    count = len(sample)
    row["median_base_qty"] = _round_base(statistics.median(sample), base)
    row["product_count"] = count
    row["package_source"] = f"Open Food Facts, median of {count} US products, {on:%Y-%m}"
    return row


def _whole(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if int(value) != value:
        return None
    return int(value)


def _existing_base_qty(ingredient: dict) -> float | None:
    quantity = _optional_float(ingredient.get("package_qty"))
    if quantity is None or quantity <= 0:
        return None
    unit = ingredient.get("package_unit") or ingredient.get("base_unit")
    if not isinstance(unit, str) or not unit:
        return None
    return size_in_base(quantity, unit, ingredient)


def accepted(row: dict, ingredient: dict) -> bool:
    """True when a reviewed row may overwrite this ingredient's package."""
    if ingredient.get("category") == PRODUCE_CATEGORY:
        return False
    noun = row.get("package_noun")
    if noun not in PACKAGE_NOUNS:
        return False
    agreeing = _whole(row.get("agreeing"))
    if agreeing is None or agreeing < AGREE_MIN:
        return False
    median = _optional_float(row.get("median_base_qty"))
    if median is None or median <= 0:
        return False
    source = row.get("package_source")
    if not isinstance(source, str) or not source.strip():
        return False
    stated = row.get("base_unit")
    if stated is not None and stated != ingredient.get("base_unit"):
        return False
    existing = _existing_base_qty(ingredient)
    if existing is None:
        return False
    ratio = median / existing
    return (1 / SIZE_RATIO) <= ratio <= SIZE_RATIO


def apply_review(catalog: dict, review: dict, *, only: set[str] | None = None) -> int:
    """Copy accepted rows onto catalog ingredients. Returns how many changed."""
    ingredients = catalog.get("ingredients")
    if not isinstance(ingredients, list):
        return 0
    by_name = {
        ingredient["name"].casefold(): ingredient
        for ingredient in ingredients
        if isinstance(ingredient, dict) and isinstance(ingredient.get("name"), str)
    }
    rows = review.get("ingredients")
    if not isinstance(rows, list):
        return 0
    updated = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        if only is not None and row["name"].casefold() not in only:
            continue
        ingredient = by_name.get(row["name"].casefold())
        if ingredient is None or not accepted(row, ingredient):
            continue
        ingredient["package_noun"] = row["package_noun"]
        ingredient["package_source"] = row["package_source"]
        ingredient["package_qty"] = row["median_base_qty"]
        base = ingredient.get("base_unit")
        if ingredient.get("package_unit") != base:
            ingredient["package_unit"] = base
        updated += 1
    return updated


def seconds_until_next(previous: float | None, *, now: float, interval: float) -> float:
    if previous is None:
        return 0.0
    return max(0.0, interval - (now - previous))


# Open Food Facts' search endpoint sheds load with 503s well under the documented rate.
OVERLOAD_STATUSES = frozenset({429, 502, 503, 504})
RETRY_DELAYS_S = (30, 60, 120)


def fetch_payload(name: str, *, timeout: float = 60) -> dict:
    request = urllib.request.Request(search_url(name), headers={"User-Agent": USER_AGENT})
    for attempt in range(len(RETRY_DELAYS_S) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in OVERLOAD_STATUSES or attempt == len(RETRY_DELAYS_S):
                return {"error": str(exc)}
            time.sleep(RETRY_DELAYS_S[attempt])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return {"error": str(exc)}
    if not isinstance(payload, dict):
        return {"error": "Open Food Facts returned no JSON object"}
    return payload


def row_from_payload(ingredient: dict, payload: dict, *, on: date) -> dict:
    if "error" in payload and "products" not in payload:
        return {"name": ingredient.get("name"), "error": str(payload["error"])}
    products = payload.get("products")
    if not isinstance(products, list):
        products = []
    return aggregate(products, ingredient, on=on)


def matching(ingredients: list, only: str | None) -> tuple[list[dict], list[str]]:
    if only is None:
        return [ing for ing in ingredients if isinstance(ing, dict)], []
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    by_name = {
        ing["name"].casefold(): ing
        for ing in ingredients
        if isinstance(ing, dict) and isinstance(ing.get("name"), str)
    }
    chosen: list[dict] = []
    missing: list[str] = []
    for name in wanted:
        ingredient = by_name.get(name.casefold())
        if ingredient is None:
            missing.append(name)
        elif ingredient not in chosen:
            chosen.append(ingredient)
    return chosen, missing


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _wait(previous: float | None) -> float:
    delay = seconds_until_next(previous, now=time.monotonic(), interval=REQUEST_INTERVAL_S)
    if delay:
        time.sleep(delay)
    return time.monotonic()


def _fetch(only: str | None) -> int:
    catalog = _read_json(INGREDIENTS_PATH)
    chosen, missing = matching(catalog.get("ingredients") or [], only)
    for name in missing:
        print(f"no catalog ingredient named {name}", file=sys.stderr)
    if only is not None and not chosen:
        return 1
    rows = []
    previous: float | None = None
    for ingredient in chosen:
        if ingredient.get("category") == PRODUCE_CATEGORY:
            print(f"{ingredient['name']}: fresh produce, no package noun", file=sys.stderr)
            continue
        previous = _wait(previous)
        payload = fetch_payload(str(ingredient["name"]))
        row = row_from_payload(ingredient, payload, on=date.today())
        rows.append(row)
        if "error" in row:
            print(f"{row['name']}: {row['error']}", file=sys.stderr)
        else:
            noun = row["package_noun"] or "none"
            print(
                f"{row['name']}: {noun}, median {row['median_base_qty']} {row['base_unit']}"
                f" ({row['agreeing']} agree, {row['product_count']} sized)",
                file=sys.stderr,
            )
    if only is not None and REVIEW_PATH.is_file():
        fetched = {row["name"] for row in rows}
        kept = [
            row
            for row in _read_json(REVIEW_PATH).get("ingredients") or []
            if row.get("name") not in fetched
        ]
        rows = kept + rows
    _write_json(REVIEW_PATH, {"ingredients": rows})
    print(f"wrote {len(rows)} rows to {REVIEW_PATH}", file=sys.stderr)
    return 0


def _apply(only: str | None) -> int:
    if not REVIEW_PATH.is_file():
        print(f"no review file at {REVIEW_PATH}", file=sys.stderr)
        return 1
    catalog = _read_json(INGREDIENTS_PATH)
    review = _read_json(REVIEW_PATH)
    names = None
    if only is not None:
        chosen, missing = matching(catalog.get("ingredients") or [], only)
        for name in missing:
            print(f"no catalog ingredient named {name}", file=sys.stderr)
        if not chosen:
            return 1
        names = {ingredient["name"].casefold() for ingredient in chosen}
    updated = apply_review(catalog, review, only=names)
    _write_json(INGREDIENTS_PATH, catalog)
    print(f"updated {updated} ingredients in {INGREDIENTS_PATH}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive package units from Open Food Facts.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="merge reviewed rows that clear the agreement gates into ingredients.json",
    )
    parser.add_argument("--only", help="comma-separated catalog names to fetch or apply")
    args = parser.parse_args(argv)
    if args.apply:
        return _apply(args.only)
    return _fetch(args.only)


if __name__ == "__main__":
    raise SystemExit(main())
