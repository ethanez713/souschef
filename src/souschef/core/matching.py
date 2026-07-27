"""Resolve free text to a canonical ingredient.

Three vocabularies arrive here and none of them agree:

    recipe   "2 tablespoons extra-virgin olive oil, divided"
    receipt  "GV EVOO 25.5Z          8.47 F"
    user     "olive oil"

Matching runs cheapest-and-most-certain first: exact alias hit, then exact name,
then fuzzy. Every layer reports the confidence and the method it used, so the UI
can quietly accept strong matches and surface weak ones for review rather than
writing a guess into the pantry.

The alias table is the part that compounds. Each correction is written back, so
the second receipt from a given store needs less fixing than the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.models import CanonicalIngredient, IngredientAlias
from souschef.models.ingredient import AliasSource

# Confidence at or above which a match is applied without asking.
AUTO_ACCEPT = 0.88
# Below this we don't even offer the match as a suggestion.
MIN_SUGGEST = 0.55


class MatchMethod(StrEnum):
    ALIAS_EXACT = "alias_exact"
    NAME_EXACT = "name_exact"
    FUZZY = "fuzzy"
    NONE = "none"


@dataclass(frozen=True)
class MatchResult:
    ingredient_id: int | None
    name: str | None
    confidence: float
    method: MatchMethod
    # Runners-up, so the review UI can offer a picklist instead of a text box.
    alternatives: tuple[tuple[int, str, float], ...] = ()

    @property
    def is_auto(self) -> bool:
        return self.ingredient_id is not None and self.confidence >= AUTO_ACCEPT

    @property
    def is_match(self) -> bool:
        return self.ingredient_id is not None


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

# Preparation and packaging words that carry no identity. Stripping them lets
# "finely chopped fresh flat-leaf parsley" reach "parsley".
_NOISE_WORDS = {
    "fresh", "freshly", "frozen", "dried", "chopped", "minced", "diced", "sliced",
    "grated", "shredded", "crushed", "ground", "whole", "large", "small", "medium",
    "extra", "organic", "raw", "cooked", "peeled", "seeded", "cored", "trimmed",
    "boneless", "skinless", "divided", "packed", "softened", "melted", "room",
    "temperature", "cold", "warm", "hot", "thinly", "roughly", "coarsely", "finely",
    "cut", "into", "pieces", "optional", "plus", "more", "for", "serving", "taste",
    "garnish", "about", "approximately", "such", "as", "preferably", "good",
    "quality", "best", "your", "favorite", "store", "bought", "homemade", "can",
    "cans", "jar", "package", "container", "bag", "box", "bunch", "head", "and",
    "or", "the", "of", "a", "an", "at", "to", "in", "with", "without", "toasted",
    "rinsed", "drained", "halved", "quartered", "lightly", "well", "very", "if",
    "needed", "desired", "reserved", "washed", "scrubbed", "stemmed",
    "torn", "shaved", "julienned", "cubed", "crumbled", "mashed", "beaten",
    "whisked", "separated", "zested", "juiced", "deveined", "butterflied",
    "pounded", "scored", "blanched", "pitted", "hulled", "shucked", "squeezed",
    "level", "heaping", "scant", "generous", "thick", "thin", "wide",
    # Measurement words. These survive quantity-stripping when written out
    # ("1 (28-ounce) can ...") and otherwise pollute the fuzzy comparison.
    "ounce", "ounces", "pound", "pounds", "gram", "grams", "kilogram", "kilograms",
    "cup", "cups", "teaspoon", "teaspoons", "tablespoon", "tablespoons", "quart",
    "quarts", "pint", "pints", "liter", "liters", "litre", "litres", "milliliter",
    "milliliters", "gallon", "gallons", "inch", "inches", "oz", "lb", "lbs", "tsp",
    "tbsp", "ml", "kg",
}

# Grocery-receipt shorthand. Stores compress aggressively and inconsistently, so
# this is seeded with the common cases and grows via the learned alias table.
_RECEIPT_ABBREVIATIONS = {
    "evoo": "extra virgin olive oil",
    "oo": "olive oil",
    "chkn": "chicken",
    "chk": "chicken",
    "bnls": "boneless",
    "skls": "skinless",
    "brst": "breast",
    "thgh": "thigh",
    "grnd": "ground",
    "gr": "ground",
    "bf": "beef",
    "prk": "pork",
    "tom": "tomato",
    "toms": "tomatoes",
    "ptto": "potato",
    "pot": "potato",
    "onn": "onion",
    "ong": "onion",
    "yel": "yellow",
    "rd": "red",
    "grn": "green",
    "wht": "white",
    "chz": "cheese",
    "chse": "cheese",
    "ched": "cheddar",
    "mozz": "mozzarella",
    "parm": "parmesan",
    "yog": "yogurt",
    "yogrt": "yogurt",
    "mlk": "milk",
    "btr": "butter",
    "brd": "bread",
    "eg": "eggs",
    "egg": "eggs",
    "lg": "large",
    "org": "organic",
    "veg": "vegetable",
    "frz": "frozen",
    "frzn": "frozen",
    "sze": "size",
    "pkt": "packet",
    "bnch": "bunch",
    "swt": "sweet",
    "ppr": "pepper",
    "pep": "pepper",
    "bll": "bell",
    "gar": "garlic",
    "gr lk": "green leaf",
    "spin": "spinach",
    "brocc": "broccoli",
    "cauli": "cauliflower",
    "mush": "mushrooms",
    "musrm": "mushrooms",
    "crm": "cream",
    "hvy": "heavy",
    "sc": "sauce",
    "sauc": "sauce",
    "sug": "sugar",
    "flr": "flour",
    "wtr": "water",
    "jc": "juice",
    "bnna": "banana",
    "ban": "banana",
    "appl": "apple",
    "avo": "avocado",
    "lmn": "lemon",
    "lm": "lime",
    "cil": "cilantro",
    "pars": "parsley",
    "sals": "salsa",
    "tort": "tortillas",
    "spag": "spaghetti",
    "past": "pasta",
    "ric": "rice",
}

# Store brand prefixes that add nothing. Kept short and explicit; a wrong strip
# is worse than a missed one.
_BRAND_PREFIXES = {
    "gv", "greatvalue", "great value", "kirkland", "ks", "signature select",
    "market pantry", "good gather", "good & gather", "365", "simple truth",
    "private selection", "trader joes", "tj", "essential everyday", "hy vee",
    "wegmans", "safeway", "kroger", "heb", "publix", "aldi", "store brand",
}

_SIZE_TOKEN = re.compile(
    r"\b\d+\.?\d*\s*(?:z|oz|ozs|lb|lbs|g|kg|ml|l|ct|pk|pc|qt|pt|gal|fz|floz)\b",
    re.IGNORECASE,
)
# A trailing tax flag is often glued to the price ("8.47F"), so the price
# pattern must not depend on a word boundary after the cents.
_PRICE = re.compile(r"\$?\d+\.\d{2}(?!\d)[A-Za-z]?")
_PLU_CODE = re.compile(r"\b\d{4,14}\b")  # UPC / PLU / item codes
_TRAILING_FLAGS = re.compile(r"\s+[A-Z]{1,2}$")  # tax flags: " F", " T", " FT"
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace. The lookup key."""
    if not text:
        return ""
    out = _NON_ALNUM.sub(" ", text.lower())
    return _WS.sub(" ", out).strip()


def _is_number(token: str) -> bool:
    return token.replace("/", "").replace(".", "").isdigit()


def head_segment(text: str) -> str:
    """The part of a recipe line before the first comma -- the product itself.

    Recipes put the ingredient first and its preparation after a comma:
    "2 large tomatoes, diced". That ordering is the only thing distinguishing
    it from "1 can diced tomatoes", since both have the identical token set
    {tomatoes, diced}. Treating everything after the comma as preparation
    resolves the ambiguity the way a cook reads it.

    Not every comma separates a product from its preparation, though: recipes
    also use them between adjectives, as in "boneless, skinless chicken thighs".
    Splitting there would leave "boneless" as the product. The test for a real
    split is whether the head still contains a content word once generic prep
    and packaging words are removed -- "2 large tomatoes" keeps "tomatoes", but
    "boneless" is nothing but a modifier and reduces to empty.
    """
    if "," not in text:
        return text.strip()

    head = text.split(",", 1)[0].strip()
    if len(head) < 3:
        return text.strip()

    # Deliberately the unprotected strip: the question is whether anything
    # survives as a *noun*, and protection exists to keep modifiers alive.
    return head if strip_noise(head) else text.strip()


def strip_noise(text: str, protected: frozenset[str] = frozenset()) -> str:
    """Remove preparation and packaging words that don't change identity.

    ``protected`` holds tokens that appear somewhere in the catalog's own
    vocabulary and must survive. Without it this function is actively dangerous:
    "canned crushed tomatoes" reduces to "tomatoes" and resolves to the fresh
    ingredient, quietly recording the wrong thing in the pantry. Words like
    "diced", "canned" and "ground" are noise in one ingredient's name and the
    entire identity of another's, so the catalog -- not a fixed list -- decides.
    """
    tokens = [
        t
        for t in normalize(text).split()
        if (t in protected or t not in _NOISE_WORDS) and not _is_number(t)
    ]
    return " ".join(tokens)


def clean_receipt_line(text: str) -> str:
    """Strip receipt furniture: prices, sizes, item codes, tax flags, brands.

    Stops short of removing preparation words -- that decision needs the catalog
    vocabulary, so it belongs to ``strip_noise``.
    """
    out = text.strip()
    out = _TRAILING_FLAGS.sub("", out)
    out = _PRICE.sub(" ", out)
    out = _SIZE_TOKEN.sub(" ", out)
    out = _PLU_CODE.sub(" ", out)
    out = normalize(out)

    for brand in _BRAND_PREFIXES:
        if out.startswith(f"{brand} "):
            out = out[len(brand) + 1 :]
            break

    # Only now, with prices, sizes and codes gone, is it safe to undo OCR's
    # digit-for-letter confusions: what remains should be product words, so
    # "EV00" -> "evoo" can no longer damage a quantity.
    from souschef.ingest.ocr import repair_confusions

    tokens = [repair_confusions(t) for t in out.split()]
    return " ".join(_RECEIPT_ABBREVIATIONS.get(t, t) for t in tokens)


def normalize_receipt_line(text: str, protected: frozenset[str] = frozenset()) -> str:
    """Clean a receipt line all the way down to a product description."""
    return strip_noise(clean_receipt_line(text), protected)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


class IngredientMatcher:
    """Matches text to canonical ingredients, caching the lookup tables.

    Built once per request/batch. The catalog is small enough (hundreds of rows)
    that holding it in memory beats round-tripping per line, and a receipt has
    dozens of lines to resolve at once.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._by_alias: dict[str, tuple[int, float]] = {}
        self._by_name: dict[str, int] = {}
        self._names: dict[int, str] = {}
        self._protected: frozenset[str] = frozenset()
        self._load()

    def _load(self) -> None:
        vocabulary: set[str] = set()

        for ing in self.session.scalars(select(CanonicalIngredient)):
            key = normalize(ing.name)
            self._by_name[key] = ing.id
            self._names[ing.id] = ing.name
            vocabulary.update(key.split())

        for alias in self.session.scalars(select(IngredientAlias)):
            existing = self._by_alias.get(alias.normalized_text)
            # A user-confirmed alias outranks a guessed one on collision.
            weight = alias.confidence + alias.times_confirmed * 0.05
            if existing is None or weight > existing[1]:
                self._by_alias[alias.normalized_text] = (alias.ingredient_id, weight)
            vocabulary.update(alias.normalized_text.split())

        # Any word the catalog itself uses is identity-bearing somewhere and is
        # never stripped as noise.
        self._protected = frozenset(vocabulary)

    def _candidates(self, text: str, is_receipt: bool) -> list[str]:
        """Identity-preserving cleaned forms, richest first.

        Only forms that keep every catalog-meaningful word belong here, because
        an exact hit on one of these is reported as certainty. The fully-stripped
        form is deliberately excluded: it destroys identity by design (reducing
        "canned crushed tomatoes" to "tomatoes"), so a hit on it is a guess, not
        a fact. It is offered to the fuzzy stage instead, where it gets scored
        rather than trusted.
        """
        forms = [normalize(text)]
        if is_receipt:
            forms.append(clean_receipt_line(text))
            forms.append(normalize_receipt_line(text, self._protected))
        else:
            # Head segment first: it isolates the product from its preparation,
            # which is what separates "tomatoes, diced" from "diced tomatoes".
            forms.append(strip_noise(head_segment(text), self._protected))
            forms.append(strip_noise(text, self._protected))

        seen: set[str] = set()
        return [f for f in forms if f and not (f in seen or seen.add(f))]

    def match(self, text: str, *, is_receipt: bool = False) -> MatchResult:
        """Resolve text to a canonical ingredient."""
        if not text or not text.strip():
            return MatchResult(None, None, 0.0, MatchMethod.NONE)

        candidates = self._candidates(text, is_receipt)
        if not candidates:
            return MatchResult(None, None, 0.0, MatchMethod.NONE)

        # Exact hits, richest form first. Alias outranks name at equal richness
        # because aliases carry learned user intent.
        for candidate in candidates:
            if candidate in self._by_alias:
                ing_id, _ = self._by_alias[candidate]
                return MatchResult(ing_id, self._names.get(ing_id), 1.0, MatchMethod.ALIAS_EXACT)
            if candidate in self._by_name:
                ing_id = self._by_name[candidate]
                return MatchResult(ing_id, self._names.get(ing_id), 1.0, MatchMethod.NAME_EXACT)

        # Fuzzy runs on the protected-strip form: noise removed, identity kept.
        cleaned = candidates[1] if len(candidates) > 1 else candidates[0]

        # 3. Fuzzy over aliases and names together.
        haystack: dict[str, int] = {k: v[0] for k, v in self._by_alias.items()}
        haystack.update(self._by_name)
        if not haystack:
            return MatchResult(None, None, 0.0, MatchMethod.NONE)

        # token_set_ratio ignores word order and extra words, which is what
        # "organic large brown eggs" vs "eggs" actually needs. Its weakness is
        # that a subset scores a perfect 100: "tomatoes" ties "crushed tomatoes"
        # against the query "crushed tomatoes", and the generic one can win on
        # arbitrary ordering. So ties break toward the more specific phrase.
        scored = process.extract(
            cleaned, haystack.keys(), scorer=fuzz.token_set_ratio, limit=25
        )

        query_tokens = set(cleaned.split())

        def specificity(phrase: str) -> int:
            """How many of the phrase's own words the query actually accounts for."""
            return len(set(phrase.split()) & query_tokens)

        ranked = sorted(
            scored,
            key=lambda item: (round(item[1], 2), specificity(item[0]), len(item[0])),
            reverse=True,
        )

        best: list[tuple[int, str, float]] = []
        seen: set[int] = set()
        for phrase, score, _ in ranked:
            ing_id = haystack[phrase]
            if ing_id in seen:
                continue
            seen.add(ing_id)
            best.append((ing_id, self._names.get(ing_id, phrase), score / 100.0))
            if len(best) >= 5:
                break

        if not best or best[0][2] < MIN_SUGGEST:
            return MatchResult(None, None, 0.0, MatchMethod.NONE, tuple(best))

        top_id, top_name, top_score = best[0]

        # A short query fuzzy-matching a long phrase is usually coincidence
        # ("oil" scoring high against "sesame oil"). Damp confidence when the
        # match relies on very little text.
        if len(cleaned) < 5:
            top_score *= 0.85

        return MatchResult(
            top_id, top_name, round(top_score, 4), MatchMethod.FUZZY, tuple(best[1:])
        )

    def learn(
        self,
        text: str,
        ingredient_id: int,
        *,
        source: AliasSource = AliasSource.MANUAL,
        is_receipt: bool = False,
    ) -> IngredientAlias:
        """Record that ``text`` means ``ingredient_id``.

        Called whenever the user confirms or corrects a match. This is what makes
        the next receipt from the same store cheaper to process.
        """
        key = (
            normalize_receipt_line(text, self._protected) if is_receipt else normalize(text)
        )
        if not key:
            key = normalize(text)

        existing = self.session.scalar(
            select(IngredientAlias).where(
                IngredientAlias.normalized_text == key,
                IngredientAlias.ingredient_id == ingredient_id,
            )
        )
        if existing is not None:
            existing.times_confirmed += 1
            existing.confidence = min(1.0, existing.confidence + 0.05)
            if source == AliasSource.MANUAL:
                existing.source = source
            self._by_alias[key] = (
                ingredient_id,
                existing.confidence + existing.times_confirmed * 0.05,
            )
            return existing

        alias = IngredientAlias(
            ingredient_id=ingredient_id,
            normalized_text=key,
            raw_text=text[:200],
            source=source,
            confidence=1.0 if source == AliasSource.MANUAL else 0.8,
            times_confirmed=1 if source == AliasSource.MANUAL else 0,
        )
        self.session.add(alias)
        self._by_alias[key] = (ingredient_id, alias.confidence)
        return alias
