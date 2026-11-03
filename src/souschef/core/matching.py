"""Resolve a recipe line to a canonical ingredient.

A recipe line and the name a cook types do not agree:

    recipe   "2 tablespoons extra-virgin olive oil, divided"
    user     "olive oil"

Matching runs cheapest-and-most-certain first: a correction the user made, then
the catalog's own name, then another alias, then fuzzy. A name the user typed
stops at the exact steps. Every layer reports the confidence and the method it
used, so the UI can quietly accept strong matches and surface weak ones for
review rather than writing a guess into the pantry.

The alias table is the part that compounds. Each correction is written back, so
the next line written the same way needs no fixing.
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

# "long-grain" is a rice variety the catalog already names ("long grain rice",
# "white rice"). Both words are in the catalog vocabulary, so the ordinary
# noise strip keeps them, and "long grain white rice" then covers neither alias.
_VARIETY_WORDS = frozenset({"long", "grain"})

# A slash between words is "cornflour / corn starch", not a fat ratio ("80/20").
_ALTERNATIVE_SPLIT = re.compile(
    r"(?<!\d)\s*/\s*(?!\d)|\s*,\s*or\s+|\s+or\s+",
    re.IGNORECASE,
)
_PAREN = re.compile(r"\(+([^)]*)\)+")
_OR_NOTE = re.compile(r"or\s+(.+)$", re.IGNORECASE)

_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


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
    """The token plus the plural shapes recipe lines actually use."""
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

    A token may be the candidate's own words written together, including a
    preparation word the content filter would otherwise drop.
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
    that holding it in memory beats round-tripping per line.
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
            if _alias_is_user(alias):
                owned = self._user_alias.get(alias.normalized_text)
                if owned is None or weight > owned[1]:
                    self._user_alias[alias.normalized_text] = (alias.ingredient_id, weight)
            vocabulary.update(alias.normalized_text.split())

        # Any word the catalog itself uses is identity-bearing somewhere and is
        # never stripped as noise.
        self._protected = frozenset(vocabulary)

    def _candidates(self, text: str) -> list[str]:
        """Identity-preserving cleaned forms, richest first.

        Only forms that keep every catalog-meaningful word belong here, because
        an exact hit on one of these is reported as certainty. The fully-stripped
        form is deliberately excluded: it destroys identity by design (reducing
        "canned crushed tomatoes" to "tomatoes"), so a hit on it is a guess, not
        a fact. It is offered to the fuzzy stage instead, where it gets scored
        rather than trusted.
        """
        forms = [normalize(text)]
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

    def match(self, text: str) -> MatchResult:
        if not text or not text.strip():
            return MatchResult(None, None, 0.0, MatchMethod.NONE)

        phrases = alternatives(text)
        best_fuzzy: MatchResult | None = None
        for phrase in phrases:
            candidates = self._candidates(phrase)
            for candidate in candidates:
                hit = self._hit(candidate)
                if hit is not None:
                    return hit
            if not candidates:
                continue
            # The head of a recipe line is the product; the words after the comma
            # are preparation, and keeping them makes "tomatoes, diced" look like
            # canned diced tomatoes.
            cleaned = candidates[1] if len(candidates) > 1 else candidates[0]
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
    ) -> IngredientAlias:
        """Record that ``text`` means ``ingredient_id``.

        Called whenever the user confirms or corrects a match, so the same wording
        resolves exactly the next time.
        """
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
