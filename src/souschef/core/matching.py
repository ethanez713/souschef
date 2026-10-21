"""Resolve free text to a canonical ingredient.

Three vocabularies arrive here and none of them agree:

    recipe   "2 tablespoons extra-virgin olive oil, divided"
    receipt  "GV EVOO 25.5Z          8.47 F"
    user     "olive oil"

Matching runs cheapest-and-most-certain first: a correction the user made, then
the catalog's own name, then another alias, then fuzzy. A name the user typed
stops at the exact steps. Every layer reports the confidence and the method it
used, so the UI can quietly accept strong matches and surface weak ones for
review rather than writing a guess into the pantry.

The alias table is the part that compounds. Each correction is written back, so
the second receipt from a given store needs less fixing than the first.
"""

from __future__ import annotations

import re
import unicodedata
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
    "grade",
    "torn", "shaved", "julienned", "cubed", "crumbled", "mashed", "beaten",
    "whisked", "separated", "zested", "juiced", "deveined", "butterflied",
    "pounded", "scored", "blanched", "pitted", "hulled", "shucked", "squeezed",
    "level", "heaping", "scant", "generous", "thick", "thin", "wide",
    # Variety and doneness words. "long-grain" and "uncooked" describe the
    # rice; they are not a different food. "wedge" is how a lemon was cut.
    "uncooked", "unsoaked", "wedge", "wedges",
    # "juice of 1 lemon" is a lemon. There is no separate juice in the catalog,
    # and leaving the word on makes "pomegranate juice" look like whatever juice
    # scored highest.
    "juice", "juices",
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
    "shrd": "shredded",
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
    "fzn": "frozen",
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
    "whip": "whipping",
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
    # I and F are the same stroke once a receipt is creased.
    "izn": "frozen",
}

# "long-grain" is a rice variety the catalog already names ("long grain rice",
# "white rice"). Both words are in the catalog vocabulary, so the ordinary
# noise strip keeps them, and "long grain white rice" then covers neither alias.
_VARIETY_WORDS = frozenset({"long", "grain"})

# Store brand prefixes that add nothing. Kept short and explicit; a wrong strip
# is worse than a missed one.
_BRAND_PREFIXES = {
    "gv", "greatvalue", "great value", "kirkland", "ks", "signature select",
    "market pantry", "good gather", "good & gather", "365", "simple truth",
    "private selection", "trader joes", "tj", "essential everyday", "hy vee",
    "wegmans", "safeway", "kroger", "heb", "publix", "aldi", "store brand",
    "kraft", "clif",
}

_SIZE_TOKEN = re.compile(
    r"\b\d+\.?\d*\s*(?:z|oz|ozs|lbs|lb|1bs|1b|ib|g|kg|ml|l|ct|pk|pc|qt|pt|gal|fz|floz)\b"
    r"|(?<=[A-Za-z])\d+\s*(?:lbs|lb|1bs|1b|ib|ct|pk|oz|z)\b"
    r"|(?<=[A-Za-z])I(?:LB|B)\b",
    re.IGNORECASE,
)
# A trailing tax flag is glued to the price ("8.47F"), so the cents are not
# always followed by a boundary. A unit is glued too ("2.34lb"); taking that
# first letter leaves a stray token the food no longer matches. One letter,
# and only when another letter does not follow. A leading dot (".29") is a
# price whose dollar digit the scanner dropped.
_PRICE = re.compile(r"\$?(?:\d+\.\d{2}|\.\d{2})(?!\d)(?:[A-Za-z](?![A-Za-z]))?")
# UPCs are often glued to a one- or two-letter tax flag ("078742362690F").
# The flag is not a word boundary, so a bare digit run would leave it behind
# and the product would no longer match. The letters are only consumed when
# a boundary follows them, so a word glued on ("040110BANANAS") stays intact.
_PLU_CODE = re.compile(r"\b\d{4,14}(?!\d)[A-Za-z]{0,2}\b")
# Tax flags are a trailing F or T. Any two letters would also eat a unit
# ("24 OZ" lost the OZ and kept a stray 24).
_TRAILING_FLAGS = re.compile(r"\s+(?:FT|TX|F|T)$", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"\b\d+\b")
# A slash between words is "cornflour / corn starch", not a fat ratio ("80/20").
_ALTERNATIVE_SPLIT = re.compile(
    r"(?<!\d)\s*/\s*(?!\d)|\s*,\s*or\s+|\s+or\s+",
    re.IGNORECASE,
)
_PAREN = re.compile(r"\(+([^)]*)\)+")
_OR_NOTE = re.compile(r"or\s+(.+)$", re.IGNORECASE)

# Receipt lines lead with a department or a flavor the catalog names without.
# Applied only after prices and abbreviations are gone, and only as a rewrite
# to a phrase the catalog already has -- not as a fuzzier score.
_RECEIPT_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsalad\s+spinach\s+baby\b"), "baby spinach"),
    (re.compile(r"\bspinach\s+baby\b"), "baby spinach"),
    (re.compile(r"\bpasta\s+organic\s+spaghetti\b"), "spaghetti"),
    (re.compile(r"\bpasta\s+spaghetti\b"), "spaghetti"),
    (re.compile(r"\bfrozen\s+organic\s+spinach\b"), "frozen spinach"),
    (re.compile(r"\bfrozen\s+organic\s+peas\b"), "frozen peas"),
    (re.compile(r"\bpasta\s+sauce\s+organic\s+(?:vodka|bolognese)\b"), "pasta sauce"),
    (re.compile(r"\bpasta\s+sauce\s+(?:vodka|bolognese)\b"), "pasta sauce"),
    (re.compile(r"\bcrunchy\s+peanut\s+butter\b"), "peanut butter"),
    (re.compile(r"\borange\s+original\s+from\s+concentra\w*\b"), "orange"),
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")

# A prepared or branded food can share a word with a staple ("peanut butter"
# bar, "deviled" egg) and still be the wrong pantry item. It may be proposed.
# It is not near-certain, so it must not start ticked.
_PREPARED_WORDS = frozenset({
    "bar", "bars", "chip", "chips", "cookie", "cookies", "deviled",
    "brownie", "brownies", "snap", "snaps", "candy", "candies",
    "burger", "burgers", "cracker", "crackers", "gyoza", "snack", "snacks",
    "dip", "dips",
})
# Store brands add no identity. A product brand ("clif", "kraft") does: the
# line is that company's food, not the staple it shares a word with.
_STORE_BRAND_TOKENS = frozenset({
    "gv", "great", "value", "greatvalue", "kirkland", "ks", "signature", "select",
    "pantry", "gather", "365", "simple", "truth", "private", "selection",
    "essential", "everyday", "hy", "vee", "wegmans", "safeway", "kroger",
    "heb", "publix", "aldi", "tj", "trader", "joes", "store", "brand", "market",
})
_BLOCKING_BRANDS = frozenset(
    token
    for brand in _BRAND_PREFIXES
    for token in brand.split()
    if token not in _STORE_BRAND_TOKENS and token not in _NOISE_WORDS
)

# OCR substitutions that show up on thermal paper. An edit that is not one of
# these is still distance 1, but a substitution outside the set is rejected so
# "chips" cannot become a different food by one unlucky letter.
_OCR_CONFUSIONS = frozenset({
    frozenset("it"), frozenset("cg"), frozenset("uo"), frozenset("il"),
    frozenset("0o"), frozenset("1l"), frozenset("1i"),
})


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace. The lookup key."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    out = _NON_ALNUM.sub(" ", folded.lower())
    return _WS.sub(" ", out).strip()


def _head_noun(phrase: str) -> str:
    """The product noun a cook would carry onto the other side of an 'or'."""
    head = head_segment(phrase)
    tokens = [
        token for token in normalize(head).split()
        if token not in _NOISE_WORDS and not _is_number(token)
    ]
    return tokens[-1] if tokens else ""


def alternatives(text: str) -> list[str]:
    """Each food a line offers, preferred first.

    Recipes write "white or yellow onion", "cornflour / corn starch" and
    "white pepper (or black)" for one slot on the shopping list. Matching the
    whole line demands one catalog row contain every alternative, so none of
    them hit. Parenthetical amounts and notes ("(uncooked)", "(200 g)") are
    not alternatives.
    """
    notes: list[str] = []

    def _paren(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        found = _OR_NOTE.match(inner)
        if found:
            notes.append(found.group(1).strip())
        return " "

    without = _WS.sub(" ", _PAREN.sub(_paren, text)).strip(" ,")
    parts = [part.strip(" ,") for part in _ALTERNATIVE_SPLIT.split(without) if part.strip(" ,")]
    if not parts:
        parts = [without or text.strip()]

    if len(parts) >= 2:
        noun = _head_noun(parts[-1])
        # Try the words as written before borrowing the other side's noun.
        # "cornflour / corn starch" is two names; "white or yellow onion" is
        # one noun said once. The borrowed form is a second candidate, so a
        # real name on the left still hits before the expanded guess.
        expanded: list[str] = []
        for index, part in enumerate(parts):
            expanded.append(part)
            if noun and index < len(parts) - 1 and noun not in normalize(part).split():
                expanded.append(f"{part} {noun}")
        parts = expanded

    primary_noun = _head_noun(parts[0]) if parts else ""
    for note in notes:
        tokens = normalize(note).split()
        if primary_noun and len(tokens) == 1 and primary_noun not in tokens:
            parts.append(f"{note} {primary_noun}")
        elif note:
            parts.append(note)

    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        key = normalize(part)
        if key and key not in seen:
            seen.add(key)
            ordered.append(part)
    return ordered or [text.strip()]


# Written-out counts ("one 14.5 ounce can") are amounts, same as digits.
_NUMBER_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
})


def _is_number(token: str) -> bool:
    """Digits, written counts, and size fragments such as ``a12ct`` or ``80z``."""
    if token in _NUMBER_WORDS:
        return True
    return any(character.isdigit() for character in token)


# A measure beside a food word is an amount ("2 cloves garlic"). A line that is
# only that word can be the food itself — the spice cloves — so it is dropped
# only when something else on the line is left to identify.
_MEASURE_WORDS = frozenset({
    "clove", "cloves", "slice", "slices", "stalk", "stalks", "sprig", "sprigs",
    "piece", "bulb", "bulbs", "count", "each",
})


def _content_tokens(text: str) -> list[str]:
    tokens = [
        t for t in text.split() if t and t not in _NOISE_WORDS and not _is_number(t)
    ]
    food = [t for t in tokens if t not in _MEASURE_WORDS]
    return food or tokens


def _token_forms(token: str) -> set[str]:
    """The token plus the plural shapes recipes and receipts actually use."""
    forms = {token}
    if len(token) > 3 and token.endswith("ies"):
        forms.add(token[:-3] + "y")
    if len(token) > 3 and token.endswith("es"):
        forms.add(token[:-2])
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        forms.add(token[:-1])
    return forms


def _token_covered(token: str, have: set[str], phrase: str) -> bool:
    """A query word is accounted for when the candidate spells it.

    Receipt OCR sometimes jams the words together (``DICEDTOMATOES``). That
    token is the candidate's own words concatenated, including a preparation
    word the content filter would otherwise drop.
    """
    forms = _token_forms(token)
    if not forms.isdisjoint(have):
        return True
    parts = [part for part in phrase.split() if part and not _is_number(part)]
    for start in range(len(parts)):
        joined = ""
        for part in parts[start:]:
            joined += part
            if len(joined) > len(token):
                break
            if not forms.isdisjoint(_token_forms(joined)):
                return True
    return False


def _covers(query: str, phrase: str) -> bool:
    """True when every content word of the query is a word of the candidate.

    ``token_set_ratio`` scores a shared modifier as a perfect match, so
    "ground coffee" ties "ground beef" and "ginger snaps" ties "ginger". A
    candidate that doesn't account for the query's own words is a different
    food, and returning it writes the wrong thing into the pantry.
    """
    wanted = _content_tokens(query)
    if not wanted:
        return False
    have: set[str] = set()
    for token in _content_tokens(phrase):
        have |= _token_forms(token)
    return all(_token_covered(token, have, phrase) for token in wanted)


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


def strip_variety(text: str) -> str:
    """Drop rice-variety words once the grain itself is still named."""
    tokens = [token for token in text.split() if token not in _VARIETY_WORDS]
    if not tokens or len(tokens) == len(text.split()):
        return ""
    return " ".join(tokens)


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


def _apply_receipt_phrases(text: str) -> str:
    """Rewrite a cleaned line onto a phrase the catalog already names.

    Kept off the collapse key. "pasta sauce vodka" and "pasta sauce bolognese"
    are both tomato sauce, but they are different purchases: folding them into
    one description made two sauces look like a quantity of 2.
    """
    for pattern, replacement in _RECEIPT_PHRASES:
        text = pattern.sub(replacement, text)
    return _WS.sub(" ", text).strip()


def _repair_vocab(extra: frozenset[str] | None) -> set[str]:
    words = set(_NOISE_WORDS)
    words.update(_RECEIPT_ABBREVIATIONS)
    for expanded in _RECEIPT_ABBREVIATIONS.values():
        words.update(expanded.split())
    if extra:
        words.update(extra)
    return {word for word in words if len(word) >= 4}


def _ocr_edit_ok(left: str, right: str) -> bool:
    """One OCR slip: a confusion substitution, or a single inserted letter."""
    if abs(len(left) - len(right)) > 1 or left == right:
        return False
    if len(left) == len(right):
        diffs = [pair for pair in zip(left, right, strict=True) if pair[0] != pair[1]]
        return len(diffs) == 1 and frozenset(diffs[0]) in _OCR_CONFUSIONS
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def _only_neighbor(token: str, vocab: set[str]) -> str | None:
    found: str | None = None
    for word in vocab:
        if abs(len(word) - len(token)) > 1 or not _ocr_edit_ok(token, word):
            continue
        if found is not None:
            return None
        found = word
    return found


def _repairs_agree(left: tuple[str, str], right: tuple[str, str]) -> bool:
    return all(a == b or _ocr_edit_ok(a, b) for a, b in zip(left, right, strict=True))


def _split_ocr_token(token: str, vocab: set[str]) -> str | None:
    """Break a jammed token into two catalog words ("brussels" + "sprouts").

    "brussel" and "brussels" are one repair read at two cuts, so they agree.
    Two cuts that name different foods do not, and the token stays as printed.
    """
    options: list[tuple[str, str]] = []
    for cut in range(4, len(token) - 3):
        left, right = token[:cut], token[cut:]
        left_word = left if left in vocab else _only_neighbor(left, vocab)
        right_word = right if right in vocab else _only_neighbor(right, vocab)
        if not left_word or not right_word:
            continue
        pair = (left_word, right_word)
        if pair not in options:
            options.append(pair)
    if not options or any(not _repairs_agree(options[0], other) for other in options[1:]):
        return None
    best = max(options, key=lambda pair: (len(pair[0]) + len(pair[1]), pair))
    return f"{best[0]} {best[1]}"


def _repair_ocr_token(token: str, vocab: set[str]) -> str:
    if len(token) < 5 or token in vocab:
        return token
    neighbor = _only_neighbor(token, vocab)
    if neighbor is not None:
        return neighbor
    if len(token) >= 10:
        return _split_ocr_token(token, vocab) or token
    return token


def _repair_ocr_tokens(tokens: list[str], vocabulary: frozenset[str] | None) -> list[str]:
    vocab = _repair_vocab(vocabulary)
    repaired: list[str] = []
    for token in tokens:
        piece = _repair_ocr_token(token, vocab)
        repaired.extend(piece.split())
    return repaired


def _expand_receipt_tokens(text: str, vocabulary: frozenset[str] | None) -> list[str]:
    """Prices and codes gone; abbreviations and OCR slips undone. No phrase rewrite."""
    out = _PRICE.sub(" ", text.strip())
    out = _TRAILING_FLAGS.sub("", out.rstrip())
    out = _SIZE_TOKEN.sub(" ", out)
    out = _PLU_CODE.sub(" ", out)
    out = normalize(out)
    from souschef.ingest.ocr import repair_confusions

    expanded: list[str] = []
    for token in (repair_confusions(part) for part in out.split()):
        expanded.extend(_RECEIPT_ABBREVIATIONS.get(token, token).split())
    return [token for token in _repair_ocr_tokens(expanded, vocabulary) if len(token) > 1]


def clean_receipt_line(text: str, vocabulary: frozenset[str] | None = None) -> str:
    """Strip receipt furniture: prices, sizes, item codes, tax flags, brands.

    Stops short of removing preparation words -- that decision needs the catalog
    vocabulary, so it belongs to ``strip_noise``. Phrase rewrites that collapse
    two products onto one catalog name are applied later, and only for matching.
    """
    tokens = _expand_receipt_tokens(text, vocabulary)
    for brand in _BRAND_PREFIXES:
        brand_tokens = brand.split()
        if tokens[: len(brand_tokens)] == brand_tokens:
            tokens = tokens[len(brand_tokens) :]
            break
    out = _BARE_NUMBER.sub(" ", " ".join(tokens))
    return _WS.sub(" ", out).strip()


def normalize_receipt_line(text: str, protected: frozenset[str] = frozenset()) -> str:
    """Clean a receipt line all the way down to a product description."""
    return strip_noise(clean_receipt_line(text, protected), protected)


def _receipt_identity_tokens(text: str, vocabulary: frozenset[str] | None) -> list[str]:
    """Words a pre-tick has to account for. Brands and prep words stay visible."""
    return [
        token
        for token in _expand_receipt_tokens(text, vocabulary)
        if token not in _NOISE_WORDS
        and token not in _STORE_BRAND_TOKENS
        and token not in _MEASURE_WORDS
        and not _is_number(token)
    ]


def _alias_is_user(alias: IngredientAlias) -> bool:
    """True when a person confirmed the alias, not when the catalog shipped it.

    Recipe import writes aliases for lines it already matched. Those must not
    outrank a catalog name: otherwise a seed row for "smoked paprika" keeps
    resolving to paprika after smoked paprika becomes its own ingredient.
    """
    return alias.source in (AliasSource.MANUAL, AliasSource.RECEIPT) or alias.times_confirmed > 0


class IngredientMatcher:
    """Matches text to canonical ingredients, caching the lookup tables.

    Built once per request/batch. The catalog is small enough (hundreds of rows)
    that holding it in memory beats round-tripping per line, and a receipt has
    dozens of lines to resolve at once.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._by_alias: dict[str, tuple[int, float]] = {}
        # A correction the user actually made. It outranks a catalog name, so
        # teaching "onion" to mean shallot sticks. A seed alias does not: the
        # catalog's own name "smoked paprika" must not resolve back to paprika.
        self._user_alias: dict[str, tuple[int, float]] = {}
        self._by_name: dict[str, int] = {}
        self._names: dict[int, str] = {}
        self._protected: frozenset[str] = frozenset()
        self._phrase_tokens: dict[int, set[str]] = {}
        self._load()

    def _load(self) -> None:
        vocabulary: set[str] = set()

        for ing in self.session.scalars(select(CanonicalIngredient)):
            key = normalize(ing.name)
            self._by_name[key] = ing.id
            self._names[ing.id] = ing.name
            vocabulary.update(key.split())
            self._phrase_tokens.setdefault(ing.id, set()).update(key.split())

        for alias in self.session.scalars(select(IngredientAlias)):
            existing = self._by_alias.get(alias.normalized_text)
            # A user-confirmed alias outranks a guessed one on collision.
            weight = alias.confidence + alias.times_confirmed * 0.05
            if existing is None or weight > existing[1]:
                self._by_alias[alias.normalized_text] = (alias.ingredient_id, weight)
            if _alias_is_user(alias):
                owned = self._user_alias.get(alias.normalized_text)
                if owned is None or weight > owned[1]:
                    self._user_alias[alias.normalized_text] = (alias.ingredient_id, weight)
            vocabulary.update(alias.normalized_text.split())
            self._phrase_tokens.setdefault(alias.ingredient_id, set()).update(
                alias.normalized_text.split()
            )

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
            cleaned = clean_receipt_line(text, self._protected)
            phrased = _apply_receipt_phrases(cleaned)
            forms.append(cleaned)
            forms.append(phrased)
            forms.append(strip_noise(phrased, self._protected))
        else:
            # Head segment first: it isolates the product from its preparation,
            # which is what separates "tomatoes, diced" from "diced tomatoes".
            forms.append(strip_noise(head_segment(text), self._protected))
            forms.append(strip_noise(text, self._protected))
            # "kosher salt and freshly ground pepper" names two foods. The match
            # is the first; a candidate cannot cover both, so the line would
            # otherwise match nothing.
            primary, separator, _rest = text.partition(" and ")
            if separator:
                forms.append(strip_noise(head_segment(primary), self._protected))
            # Variety words are protected (they appear in "long grain rice"), so
            # the noise strip cannot drop them. A second form without them lets
            # "long-grain white rice" hit "white rice" exactly.
            varied = strip_variety(forms[-1] if forms else "")
            if varied:
                forms.append(varied)

        seen: set[str] = set()
        return [
            f
            for f in forms
            if f and not (f in seen or seen.add(f))  # type: ignore[func-returns-value]
        ]

    def _hit(self, key: str) -> MatchResult | None:
        """Exact resolution: a user correction, else the catalog name, else an alias."""
        if not key:
            return None
        owned = self._user_alias.get(key)
        if owned is not None:
            ing_id = owned[0]
            return MatchResult(ing_id, self._names.get(ing_id), 1.0, MatchMethod.ALIAS_EXACT)
        if key in self._by_name:
            ing_id = self._by_name[key]
            return MatchResult(ing_id, self._names.get(ing_id), 1.0, MatchMethod.NAME_EXACT)
        aliased = self._by_alias.get(key)
        if aliased is not None:
            ing_id = aliased[0]
            return MatchResult(ing_id, self._names.get(ing_id), 1.0, MatchMethod.ALIAS_EXACT)
        return None

    def match_exact(self, text: str) -> MatchResult:
        """Resolve a name the user typed. Never a fuzzy guess.

        "This is" / "Wrong match" is the user naming the food. Fuzzy-matching
        that text back onto the row they are correcting is how "smoked paprika"
        stayed paprika with no message.
        """
        hit = self._hit(normalize(text))
        if hit is not None:
            return hit
        return MatchResult(None, None, 0.0, MatchMethod.NONE)

    def receipt_near_certain(self, text: str, ingredient_id: int) -> bool:
        """True when every word on the line is the ingredient, not a product of it.

        An automatic match is not enough to tick the box. "Crunchy peanut butter"
        on a bar, and "deviled egg" from the deli, both share a staple's name.
        """
        tokens = _receipt_identity_tokens(text, self._protected)
        if not tokens or any(token in _PREPARED_WORDS for token in tokens):
            return False
        if tokens[0] in _BLOCKING_BRANDS:
            return False
        covered: set[str] = set()
        for word in self._phrase_tokens.get(ingredient_id, ()):
            covered |= _token_forms(word)
        if not all(_token_forms(token) & covered for token in tokens):
            return False
        # "sprouts" is a word of "brussels sprouts", and that is not the food.
        # The line has to carry every content word of the canonical name.
        name = normalize(self._names.get(ingredient_id, ""))
        name_tokens = [
            token
            for token in name.split()
            if token not in _NOISE_WORDS
            and token not in _MEASURE_WORDS
            and not _is_number(token)
        ]
        line_forms: set[str] = set()
        for token in tokens:
            line_forms |= _token_forms(token)
        return bool(name_tokens) and all(
            _token_forms(token) & line_forms for token in name_tokens
        )

    def _fuzzy(self, cleaned: str) -> MatchResult:
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

        def extra_tokens(phrase: str) -> int:
            # token_set_ratio scores a subset as 100, so "red pepper" ties
            # "red pepper flakes". Prefer the phrase that doesn't invent words.
            return len(set(phrase.split()) - query_tokens)

        ranked = sorted(
            (
                item for item in scored
                if _covers(cleaned, item[0])
            ),
            key=lambda item: (
                round(item[1], 2),
                specificity(item[0]),
                -extra_tokens(item[0]),
                -len(item[0]),
            ),
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

    def match(self, text: str, *, is_receipt: bool = False) -> MatchResult:
        if not text or not text.strip():
            return MatchResult(None, None, 0.0, MatchMethod.NONE)

        # Receipt lines don't carry "A or B". Trying that split on a register
        # tape would invent foods from a department code.
        phrases = [text] if is_receipt else alternatives(text)
        best_fuzzy: MatchResult | None = None
        for phrase in phrases:
            candidates = self._candidates(phrase, is_receipt)
            for candidate in candidates:
                hit = self._hit(candidate)
                if hit is not None:
                    return hit
            if not candidates:
                continue
            # The head of a recipe line is the product; the words after the comma
            # are preparation, and keeping them makes "tomatoes, diced" look like
            # canned diced tomatoes. A receipt has no such split, so the fuzzy
            # query is the line with prices and noise words removed.
            if is_receipt:
                cleaned = candidates[-1]
            elif len(candidates) > 1:
                cleaned = candidates[1]
            else:
                cleaned = candidates[0]
            fuzzy = self._fuzzy(cleaned)
            if fuzzy.is_match and (best_fuzzy is None or fuzzy.confidence > best_fuzzy.confidence):
                best_fuzzy = fuzzy

        return best_fuzzy or MatchResult(None, None, 0.0, MatchMethod.NONE)

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
            self._remember(key, ingredient_id, existing)
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
        self._remember(key, ingredient_id, alias)
        return alias

    def _remember(self, key: str, ingredient_id: int, alias: IngredientAlias) -> None:
        weight = alias.confidence + alias.times_confirmed * 0.05
        self._by_alias[key] = (ingredient_id, weight)
        if _alias_is_user(alias):
            self._user_alias[key] = (ingredient_id, weight)
