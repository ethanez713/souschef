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

# A row is furniture when every word on it is one of these. A substring check
# dropped "TOTAL WHEAT BREAD"; requiring the whole row to be furniture keeps
# that loaf and still drops "TOTAL", "TAX" and "THANK YOU FOR SHOPPING".
_FURNITURE = frozenset({
    "sub", "subtotal", "total", "tax", "change", "due", "cash", "credit", "debit",
    "visa", "mastercard", "amex", "discover", "balance", "tender", "tendered",
    "tend", "payment", "auth", "approved", "purchase", "ref", "terminal", "merchant",
    "cashier", "reg", "register", "thank", "you", "for", "shopping", "survey",
    "savings", "discount", "coupon", "loyalty", "rewards", "points", "return",
    "policy", "receipt", "item", "items", "sold", "phone", "open", "hours",
    "manager", "mgr", "bag", "fee", "deposit", "grand", "amount", "paid", "card",
    "number", "customer", "copy", "member", "transaction", "approval", "code",
    "please", "visit", "feedback", "master", "usd", "dollar", "dollars",
    "void", "voided", "entry", "signature", "required", "method", "chip",
    "appr", "seq", "aid", "no", "yes",
    # Slogans and footers. A line is furniture only when every word is in here,
    # so "low" does not drop "low fat milk".
    "introducing", "plus", "podcast", "podcasts", "shopper", "low", "price",
    "prices", "trust", "every", "day", "can", "our", "join", "today",
})
# A header whose words are all a store's name. It is not a product, but it is
# still the name detect_store should record. "JOE COFFEE ..." has extra words,
# so it is not a header. Glued OCR ("TRADERJOE'S") is one token.
_STORE_HEADER = frozenset({
    "walmart", "target", "costco", "kroger", "aldi", "trader", "joe", "joes",
    "safeway", "publix", "wegmans", "heb", "meijer", "supercenter", "market",
    "neighborhood", "store", "foods", "whole", "lidl", "wholefoods",
    "wm", "traderjoe", "traderjoes",
})
# Distinctive chains. "sprouts" is not one of them and not a header word: a
# line that only says SPROUTS is a purchase. "joe" alone is the store.
_CHAINS: tuple[tuple[str, str], ...] = (
    ("traderjoes", "TRADER JOE'S"),
    ("traderjoe", "TRADER JOE'S"),
    ("wholefoods", "WHOLE FOODS"),
    ("walmart", "WALMART"),
    ("target", "TARGET"),
    ("kroger", "KROGER"),
    ("costco", "COSTCO"),
    ("safeway", "SAFEWAY"),
    ("publix", "PUBLIX"),
    ("wegmans", "WEGMANS"),
    ("meijer", "MEIJER"),
    ("aldi", "ALDI"),
    ("lidl", "LIDL"),
)
_TOTAL_WORDS = frozenset({"total", "purchase", "amount", "due", "grand", "balance"})
# Show-through from the back of the paper, reversed. "UOY XMAHT" is "THANK YOU"
# with two strokes misread; it must not become the store name.
_MIRROR_PHRASES = (
    "thankyou", "walmart", "traderjoes", "target", "costco", "kroger",
    "safeway", "publix", "wholefoods", "subtotal",
)

# A card number, or the masked last digits stores print after VISA / CARD.
# Consecutive digits only. A separator after every digit also swallowed a UPC
# plus the first digit of the price ("305210179000 2.82") and hid the product.
_CARD_NUMBER = re.compile(r"\d{13,19}")
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

# "123 MAIN ST" is an address. A product row does not end in a street suffix.
_ADDRESS = re.compile(
    r"\b\d{1,6}\s+[A-Za-z].*\b(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|way|ct|pkwy)\b\.?\s*$",
    re.IGNORECASE,
)
_URL = re.compile(r"www\.|\.com\b", re.IGNORECASE)
# Authorization codes ("ARC:00") are not products. A size does not look like this.
_AUTH_CODE = re.compile(r"\b[A-Z]{2,6}\s*:\s*\d{2,}\b", re.IGNORECASE)

# A row that is only digits, punctuation and currency is a number, not an item.
_ONLY_NUMERIC = re.compile(r"^[\s\d.,:$#*/x@%-]+$")
# Dates and times. No word boundary: a glued timestamp ("09:12202104:23") still
# contains a clock reading.
_DATE_LIKE = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}:\d{2}")
# Payment and footer markers. One of these is enough; the rest of the line is
# an authorization code or a slogan, not a second product.
_STRUCTURAL = re.compile(
    r"ref\s*#|appr\s*#|terminal\s*#|card\s*#|seq\s*#|\baid\b|"
    r"\bvoid(?:ed)?\b|items?\s*sold|signature\s+required|entry\s+method|"
    r"\bid\s*#|\bthank\s*you\b|\bmgr\b|\bstore\s*#|"
    r"\btc\s*[:#]?\s*[0-9a-f]{6,}",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\d{3}\D{1,3}\d{3}\D{1,3}\d{4}")
_HEX_BLOB = re.compile(r"\b[0-9A-F]{12,}\b", re.IGNORECASE)
_UNIT_PRICE_ROW = re.compile(r"^\s*\d+\s*(?:ea|la)\b", re.IGNORECASE)

_PRICE = re.compile(r"\$?(\d*\.\d{2})(?!\d)")
# "2.34 lb @ 4.99/lb", "2.21 lb. @ 1 lb. /0.46", or "2.211b.@". "1b" is "lb"
# with the L read as a one. The quantity keeps two decimals, so the "1" is the
# unit and not a third digit of the weight.
_WEIGHED = re.compile(
    r"(\d+\.?\d*)\s*(lbs|lb|1bs|1b|ib|kg|oz|g)\.?\s*\S{0,2}\s*@",
    re.IGNORECASE,
)
# "3 @ 1.99" or "3 x 1.99" -- a multi-buy.
_MULTIPLE = re.compile(r"^\s*(\d{1,2})\s*[@x]\s*\$?\d+\.\d{2}", re.IGNORECASE)
# A size embedded in the description: "MOZZ CHZ 8OZ", "MILK 1GAL".
_SIZE = re.compile(
    r"(\d+\.?\d*)\s*(z|oz|lbs|lb|1bs|1b|ib|g|kg|ml|l|ct|pk|gal|qt|pt)\b",
    re.IGNORECASE,
)
# "SPROUISILB": the "1" of "1LB" was read as I and glued to the product word.
_GLUED_LB = re.compile(r"(?<=[A-Za-z])I(?:LB|B)\b", re.IGNORECASE)

_SIZE_UNIT_ALIASES = {
    "z": "oz", "ct": "count", "pk": "count", "1b": "lb", "1bs": "lbs", "ib": "lb",
}

# "8 OZ" reliably OCRs as "80Z" because O becomes zero. Read literally that is
# eighty ounces -- a tenfold error on every packaged good. A trailing zero
# immediately before a lone Z is always the letter, since no receipt prints a
# size in "160 z".
_OZ_CONFUSION = re.compile(r"\b(\d+(?:\.\d+)?)0Z\b", re.IGNORECASE)

# A row carrying only a weight and prices: the continuation of the item above.
_CONTINUATION = re.compile(
    r"^\s*\d+\.?\d*\s*(?:lbs|lb|1bs|1b|ib|kg|oz|g)\.?\s*\S{0,2}\s*@"
    r"|^\s*\d+\s*[@x]\s*\$?\d+\.\d{2}",
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
    # Set when the match accounts for the whole line, not just a word it shares
    # with a bar, a chip, or a deli item.
    near_certain: bool = False

    @property
    def needs_review(self) -> bool:
        if self.is_noise or self.match is None or not self.match.is_match:
            return True
        return not self.match.is_auto


def should_preconfirm(line: ParsedReceiptLine) -> bool:
    """A near-certain match starts ticked. A guess, even a confident one, does not."""
    return (
        not line.is_noise
        and line.near_certain
        and line.match is not None
        and line.match.is_auto
    )


def redact_receipt_text(text: str) -> str:
    """Remove card numbers before a receipt line is stored or shown.

    The review has to show the printed words so a person can correct a match.
    It does not have to keep a card number to do that. A shopper's name is left
    alone: guessing which capitalized word is a person eats real product lines.
    """
    text = _CARD_NUMBER.sub("[card]", text)
    return _MASKED_CARD.sub("[card]", text)


def _edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for i, ch in enumerate(left, start=1):
        current = [i]
        for j, other in enumerate(right, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ch != other),
            ))
        previous = current
    return previous[-1]


def _alpha_tokens(text: str) -> list[str]:
    folded = _for_noise_check(text).lower()
    return [token for token in re.findall(r"[a-z]+", folded) if len(token) > 1]


def _close_to_furniture(token: str) -> bool:
    if token in _FURNITURE:
        return True
    # "IOTAL" / "AFPROVED": one letter misread in place. Only a same-length
    # substitution counts, because receipt abbreviations drop letters instead
    # ("GRND" is ground beef, one deletion from "grand").
    if len(token) < 4:
        return False
    return any(
        len(token) == len(word) and _edit_distance(token, word) <= 1
        for word in _FURNITURE
        if len(word) >= 4
    )


def _is_furniture_line(text: str) -> bool:
    tokens = [token for token in _alpha_tokens(text) if len(token) > 2]
    return bool(tokens) and all(_close_to_furniture(token) for token in tokens)


def _is_store_header(text: str) -> bool:
    tokens = _alpha_tokens(text)
    return bool(tokens) and all(token in _STORE_HEADER for token in tokens)


def looks_mirrored(text: str) -> bool:
    """True when the row reads as a known phrase only once it is reversed."""
    letters = "".join(ch for ch in text.lower() if ch.isalpha())
    if len(letters) < 5:
        return False
    forward = min(_edit_distance(letters, phrase) for phrase in _MIRROR_PHRASES)
    backward = min(_edit_distance(letters[::-1], phrase) for phrase in _MIRROR_PHRASES)
    return backward <= 2 and backward < forward


def _for_noise_check(text: str) -> str:
    """Undo the OCR confusions that hide an otherwise obvious furniture word.

    ``T0TAL`` and ``GTOTAL`` are the same row as ``TOTAL``, and the noise list
    already knows that word. Replacing every zero would also rewrite prices.
    """
    upper = text.upper()
    upper = re.sub(r"(?<=[A-Z])0(?=[A-Z])", "O", upper)
    return re.sub(r"\bG+TOTAL\b", "TOTAL", upper)


def _has_price(text: str) -> bool:
    return _PRICE.search(text) is not None


def _has_size(text: str) -> bool:
    return _SIZE.search(text) is not None or _GLUED_LB.search(text) is not None


def _has_product_word(text: str) -> bool:
    return any(
        len(token) >= 4 and token not in _FURNITURE and token not in _STORE_HEADER
        for token in _alpha_tokens(text)
    )


def _has_code(text: str) -> bool:
    return re.search(r"\d{8,}", text) is not None


def _mostly_symbols(text: str) -> bool:
    """A broken header that is hashes and one-letter scraps, not a product."""
    if _has_price(text) or _has_size(text):
        return False
    hashes = text.count("#") + text.count("*")
    letters = sum(char.isalpha() for char in text)
    named = any(len(token) >= 5 for token in _alpha_tokens(text))
    if hashes >= 4 and not named:
        return True
    chars = [char for char in text if not char.isspace()]
    if len(chars) < 4:
        return False
    marks = sum(not char.isalnum() for char in chars)
    return marks >= 4 and marks >= letters and not _has_product_word(text)


def _letters(text: str) -> str:
    return "".join(char for char in text.lower() if char.isalpha())


def _chain_name(text: str) -> str | None:
    """A known chain printed as the store, not as a word inside a product."""
    if _has_price(text) or _has_size(text):
        return None
    tokens = set(_alpha_tokens(text))
    glued = _letters(text)
    if "supercenter" in tokens and ("wm" in tokens or "walmart" in glued):
        return "WALMART"
    if {"trader", "joe"} <= tokens or {"trader", "joes"} <= tokens:
        return "TRADER JOE'S"
    if {"whole", "foods"} <= tokens or "wholefoods" in tokens:
        return "WHOLE FOODS"
    for key, name in _CHAINS:
        if key in tokens or glued == key or glued.startswith(key):
            return name
    return None


def _is_thank_you(text: str) -> bool:
    tokens = _alpha_tokens(text)
    has_you = "you" in tokens
    has_thank = any(
        token == "thank" or (len(token) >= 4 and _edit_distance(token, "thank") <= 1)
        for token in tokens
    )
    return has_you and has_thank


def _is_tax_token(token: str) -> bool:
    letters = "".join(char for char in token.lower() if char.isalpha())
    if not letters or len(letters) > 6:
        return False
    if _edit_distance(letters, "tax") <= 1:
        return True
    # "ATLIAX": tax is still in there, with a few strokes added around it.
    found = 0
    for char in letters:
        if char == "tax"[found]:
            found += 1
            if found == 3:
                return len(letters) - 3 <= 3
    return False


def _is_money_line(text: str) -> bool:
    """A price whose words are all settlement words, possibly misread."""
    if not _has_price(text):
        return False
    tokens = [
        "".join(char for char in raw.lower() if char.isalpha())
        for raw in re.findall(r"[A-Za-z0-9]+", text)
    ]
    tokens = [token for token in tokens if len(token) > 1]
    if not tokens:
        return False
    return all(_close_to_furniture(token) or _is_tax_token(token) for token in tokens)


def _is_datetime_line(text: str) -> bool:
    if _DATE_LIKE.search(text) is None:
        return False
    residue = _DATE_LIKE.sub(" ", text)
    residue = re.sub(r"\d+", " ", residue)
    return sum(char.isalpha() for char in residue) < 4


def _is_hex_blob(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    if not tokens:
        return False
    hexes = [token for token in tokens if _HEX_BLOB.fullmatch(token)]
    words = [
        token for token in tokens
        if re.search(r"[A-Za-z]", token) and _HEX_BLOB.fullmatch(token) is None
        and len(re.sub(r"\d", "", token)) >= 3
    ]
    return bool(hexes) and not words


def _is_address_line(text: str) -> bool:
    if _has_price(text) or not re.search(r"\d", text):
        return False
    return _ADDRESS.search(text) is not None or re.search(
        r"\b(?:parkway|suite|highway|plaza)\b", text, re.IGNORECASE
    ) is not None


def _short_token_line(text: str) -> bool:
    """'HOM Mge Aon': no word long enough to be a product, and nothing was bought.

    A UPC means the row was scanned as a purchase even when the name is short
    ("SH FN", "NV PB"), so those stay.
    """
    if _has_price(text) or _has_size(text) or _has_code(text):
        return False
    tokens = _alpha_tokens(text)
    return bool(tokens) and all(len(token) <= 3 for token in tokens)


def _digit_heavy(text: str) -> bool:
    """A transaction id or a glued timestamp, not a named purchase."""
    if _has_price(text) or _has_size(text):
        return False
    tokens = _alpha_tokens(text)
    named = any(
        len(token) >= 5 and token not in _FURNITURE and token not in _STORE_HEADER
        for token in tokens
    )
    # A short name beside a barcode is a purchase ("NV PB" + UPC). A single
    # stub in front of a transaction number ("rans10:1852...") is not.
    if named or (_has_code(text) and len(tokens) >= 2):
        return False
    letters = sum(char.isalpha() for char in text)
    digits = sum(char.isdigit() for char in text)
    return digits >= 8 and digits > letters


# "y" counts: otherwise "oyster" looks like a consonant clump. Clusters below
# are the three-consonant shapes English actually uses. "hsm" and "gst" are not.
_VOWELS = frozenset("aeiouy")
_COMMON_CLUSTERS = frozenset({
    "spr", "str", "scr", "spl", "shr", "thr", "chr", "sch", "phr",
    "nch", "tch", "ght", "rst", "rth", "nth", "lth", "cts", "nts",
    "nds", "rds", "lds", "sts", "mps", "nks", "rks", "lks", "pts",
    "lps", "rps", "xts", "mph", "lch", "rch", "sks",
})


def _consonant_runs(token: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for char in token:
        if char in _VOWELS:
            if current:
                runs.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        runs.append("".join(current))
    return runs


def _ordinary_cluster(run: str) -> bool:
    if len(run) < 3 or any(run[i] == run[i + 1] for i in range(len(run) - 1)):
        return True
    return run[:3] in _COMMON_CLUSTERS


def _gibberish_token(token: str) -> bool:
    if len(token) < 5:
        return False
    runs = _consonant_runs(token)
    vowels = sum(char in _VOWELS for char in token)
    if vowels == 0:
        return True
    unusual = [run for run in runs if not _ordinary_cluster(run)]
    if any(len(run) >= 4 for run in unusual):
        return True
    if vowels == 1 and any(len(run) >= 3 for run in unusual):
        return True
    # "olegst" ends on a clump a real word would not.
    return (
        token[-1] not in _VOWELS
        and vowels <= 2
        and bool(runs)
        and len(runs[-1]) >= 3
        and not _ordinary_cluster(runs[-1])
    )


def _gibberish_line(text: str) -> bool:
    """Show-through is a row of non-words ("hsmisW", "olegst"), not one brand.

    "QUEST" is a single odd token and a real purchase. This junk never carries
    a price.
    """
    if _has_price(text) or _has_size(text):
        return False
    long = [token for token in _alpha_tokens(text) if len(token) >= 5]
    return len(long) >= 2 and all(_gibberish_token(token) for token in long)


def _irregular_fragment(text: str) -> bool:
    """A single mixed-case scrap ('COIl'), not a title-case product ('Bananas')."""
    if _has_price(text) or _has_size(text):
        return False
    tokens = text.split()
    if len(tokens) != 1:
        return False
    letters = [char for char in tokens[0] if char.isalpha()]
    if len(letters) < 4 or len(letters) > 6 or letters[0].islower():
        return False
    body = letters[1:]
    return any(char.isupper() for char in body) and any(char.islower() for char in body)


def _footer_fragment(text: str) -> bool:
    """One leftover token a letter or two off a footer word ('oucast')."""
    if _has_price(text) or _has_size(text):
        return False
    tokens = _alpha_tokens(text)
    if len(tokens) != 1 or len(tokens[0]) < 5:
        return False
    token = tokens[0]
    return any(
        abs(len(token) - len(word)) <= 2 and _edit_distance(token, word) <= 2
        for word in ("podcast", "podcasts", "introducing", "shopper")
    )


def is_noise_line(text: str) -> bool:
    """True for rows that are receipt furniture rather than purchases.

    A barcode is not furniture by itself. Product rows carry a UPC beside the
    name; only a row with nothing left to buy once the code is ignored is junk.
    """
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    if _ONLY_NUMERIC.match(stripped):
        return True
    if _is_datetime_line(stripped):
        return True
    if (
        (_CARD_NUMBER.search(stripped) or _MASKED_CARD.search(stripped))
        and not _has_product_word(stripped)
        and not _has_size(stripped)
    ):
        return True
    state_zip = _STATE_ZIP.search(stripped.upper())
    if state_zip and state_zip.group(1) in _STATES:
        return True
    if looks_mirrored(stripped) or _is_store_header(stripped) or _chain_name(stripped):
        return True
    if _STRUCTURAL.search(stripped) or _is_thank_you(stripped) or _PHONE.search(stripped):
        return True
    if _is_address_line(stripped) or _URL.search(stripped) or _AUTH_CODE.search(stripped):
        return True
    if _is_furniture_line(stripped) or _is_money_line(stripped):
        return True
    if _is_hex_blob(stripped) and not _has_product_word(stripped) and not _has_size(stripped):
        return True
    if _digit_heavy(stripped):
        return True
    if _UNIT_PRICE_ROW.match(stripped) or _mostly_symbols(stripped):
        return True
    if _short_token_line(stripped) or _irregular_fragment(stripped) or _footer_fragment(stripped):
        return True
    if _gibberish_line(stripped):
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

    # A trailing price is money. Strip it before reading a size so "2.69"
    # cannot be read as two of something.
    body = _PRICE.sub(" ", text)

    size = _SIZE.search(repair_size_confusions(body))
    if size:
        unit = size.group(2).lower()
        return float(size.group(1)), _SIZE_UNIT_ALIASES.get(unit, unit)

    if _GLUED_LB.search(body):
        return 1.0, "lb"

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
    description = clean_receipt_line(text, matcher._protected)

    line = ParsedReceiptLine(
        raw_text=text,
        description=description,
        quantity=quantity,
        unit=unit,
        price=price,
        ocr_confidence=confidence,
    )
    line.match = matcher.match(text, is_receipt=True)
    if line.match is not None and line.match.is_auto and line.match.ingredient_id is not None:
        line.near_certain = matcher.receipt_near_certain(text, line.match.ingredient_id)
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


def collapse_repeats(lines: list[ParsedReceiptLine]) -> list[ParsedReceiptLine]:
    """One row per product. Identical descriptions become a count, or a sum.

    Three "PASTA ORG SPAGHETTI" rows are three boxes of spaghetti, not three
    cards to tick. Rows that already carry the same unit add up instead.
    Noise rows are left as printed.
    """
    collapsed: list[ParsedReceiptLine] = []
    index: dict[str, int] = {}
    copies: dict[str, int] = {}
    for line in lines:
        key = line.description
        if line.is_noise or not key:
            collapsed.append(line)
            continue
        if key not in index:
            index[key] = len(collapsed)
            copies[key] = 1
            collapsed.append(line)
            continue
        kept = collapsed[index[key]]
        # A second OCR of one priced row differs by a misread letter and repeats
        # the price. Counting it turns those dollars into a quantity.
        if (
            kept.price is not None
            and line.price is not None
            and abs(kept.price - line.price) < 0.001
            and kept.raw_text.strip().casefold() != line.raw_text.strip().casefold()
        ):
            continue
        if (
            kept.quantity is not None
            and line.quantity is not None
            and kept.unit == line.unit
        ):
            kept.quantity += line.quantity
        elif line.quantity is None and (kept.quantity is None or kept.unit == "count"):
            copies[key] += 1
            kept.quantity = float(copies[key])
            kept.unit = "count"
        elif kept.quantity is None:
            kept.quantity = line.quantity
            kept.unit = line.unit
    return collapsed


def parse_receipt(result: OcrResult, session: Session) -> list[ParsedReceiptLine]:
    matcher = IngredientMatcher(session)
    parsed = [
        parse_line(text, confidence, matcher)
        for text, confidence in merge_continuations(result.lines)
    ]
    return collapse_repeats(parsed)


def detect_store(lines: list[OcrLine]) -> str | None:
    """The store header. A known chain beats OCR garbage; garbage is not a name."""
    from souschef.ingest.ocr import repair_line

    head = lines[:12]
    for line in head:
        name = _chain_name(line.text)
        if name and not looks_mirrored(line.text) and not _mostly_symbols(line.text):
            return name
    for line in head:
        text = line.text.strip()
        if len(text) < 4 or sum(character.isalpha() for character in text) < 3:
            continue
        if looks_mirrored(text) or _mostly_symbols(text) or _short_token_line(text):
            continue
        if is_noise_line(text):
            continue
        name = re.sub(r"[#\d]+\s*$", "", text).strip()
        return repair_line(name).upper() or None
    return None


def detect_total(lines: list[OcrLine]) -> float | None:
    """Find the row labelled TOTAL, preferring it over SUBTOTAL.

    A product that merely contains the word ("TOTAL WHEAT BREAD") is not it.
    """
    total: float | None = None
    for line in lines:
        folded = _for_noise_check(line.text)
        if re.search(r"\bsub\s*total\b", folded, re.IGNORECASE):
            continue
        tokens = _alpha_tokens(folded)
        if "total" not in tokens or not set(tokens) <= _TOTAL_WORDS:
            continue
        price = extract_price(line.text)
        if price is not None:
            total = price
    return total
