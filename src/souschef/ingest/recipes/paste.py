"""Parse a recipe someone pasted as plain text.

The first non-empty line is the title. An Ingredients heading and a
Directions, Method, Instructions or Steps heading split the body. Without
an Ingredients heading, only bullet lines are ingredients. A leading number
is a list index when a dot and a space follow it ("1. flour"), and a
quantity when they do not ("2.5 tbsp").
"""

from __future__ import annotations

import re

from souschef.ingest.recipes.jsonld import RawRecipe

# Optional leading markdown hashes, optional colon. The whole line is the heading.
_HEADING = re.compile(
    r"^#{0,6}\s*(?P<name>ingredients|directions|method|instructions|steps)\s*:?\s*$",
    re.IGNORECASE,
)
_TITLE_HASH = re.compile(r"^#{1,6}\s*")
_SERVINGS = re.compile(
    r"^(?:serves|servings|yields?|makes)\b\s*:?\s*"
    r"(?:about|approximately|around|approx\.?)?\s*(\d+)",
    re.IGNORECASE,
)
# "Cook for 10 minutes." is a step. A time label says "time" or uses a colon
# ("Cook time: 1 hour", "Prep: 15 minutes").
_TIME_LINE = re.compile(
    r"^(?P<kind>prep|preparation|cook|cooking|total)\b"
    r"(?:\s+time\b\s*:?|\s*:)\s*(?P<rest>.+)$",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[-*•]\s*")
# "1. 2 cups" is a list index. "2.5 tbsp" is a quantity: whitespace after the
# dot is what makes the number a marker rather than part of the amount.
_LIST_NUM = re.compile(r"^\d+\.\s+")
_SENTENCE_END = (".", "!", "?")


def parse_pasted_recipe(text: str) -> RawRecipe:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title, body = _title_and_body(lines)
    servings, prep, cook, total, content = _pull_meta(body)
    ingredients, steps = _split_sections(content)
    return RawRecipe(
        title=title,
        ingredients=ingredients,
        instructions="\n\n".join(steps) or None,
        servings=servings,
        total_minutes=total if total is not None else _sum_times(prep, cook),
    )


def _title_and_body(lines: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return "", []
    title = _TITLE_HASH.sub("", lines[index].strip()).strip()
    return title, lines[index + 1 :]


def _pull_meta(
    lines: list[str],
) -> tuple[int | None, int | None, int | None, int | None, list[str]]:
    servings = prep = cook = total = None
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _heading_kind(stripped):
            kept.append(line)
            continue
        serving = _SERVINGS.match(stripped)
        if serving:
            if servings is None:
                servings = int(serving.group(1))
            continue
        timed = _time_of(stripped)
        if timed is None:
            kept.append(line)
            continue
        kind, minutes = timed
        if kind == "prep" and prep is None:
            prep = minutes
        elif kind == "cook" and cook is None:
            cook = minutes
        elif kind == "total" and total is None:
            total = minutes
        # A second label of the same kind is still a label, not an ingredient.
    return servings, prep, cook, total, kept


def _time_of(line: str) -> tuple[str, int] | None:
    match = _TIME_LINE.match(line)
    if not match:
        return None
    minutes = _duration_minutes(match.group("rest"))
    if minutes is None:
        return None
    kind = match.group("kind").lower()
    if kind.startswith("prep"):
        return "prep", minutes
    if kind.startswith("cook"):
        return "cook", minutes
    return "total", minutes


def _duration_minutes(text: str) -> int | None:
    found = list(_DURATION.finditer(text))
    if not found:
        return None
    total = 0.0
    for match in found:
        value = float(match.group(1))
        unit = match.group(2).lower()
        total += value * 60 if unit.startswith("h") else value
    rounded = int(round(total))
    return rounded or None


def _sum_times(prep: int | None, cook: int | None) -> int | None:
    """A stated total is handled by the caller. Prep and cook are one clock."""
    if prep is None and cook is None:
        return None
    return (prep or 0) + (cook or 0)


def _heading_kind(line: str) -> str | None:
    match = _HEADING.match(line.strip())
    if not match:
        return None
    if match.group("name").lower() == "ingredients":
        return "ingredients"
    return "method"


def _first_heading(lines: list[str], kind: str) -> int | None:
    for index, line in enumerate(lines):
        if _heading_kind(line) == kind:
            return index
    return None


def _split_sections(lines: list[str]) -> tuple[list[str], list[str]]:
    ingredients_at = _first_heading(lines, "ingredients")
    method_at = _first_heading(lines, "method")
    if ingredients_at is None:
        return _without_ingredient_heading(lines, method_at)

    ingredient_end = len(lines)
    if method_at is not None and method_at > ingredients_at:
        ingredient_end = method_at
    ingredients = _ingredient_lines(lines[ingredients_at + 1 : ingredient_end])
    if method_at is None:
        return ingredients, []
    if method_at > ingredients_at:
        method_lines = lines[method_at + 1 :]
    else:
        method_lines = lines[method_at + 1 : ingredients_at]
    return ingredients, _steps_from(method_lines)


def _without_ingredient_heading(
    lines: list[str], method_at: int | None
) -> tuple[list[str], list[str]]:
    ingredients: list[str] = []
    method_lines: list[str] = []
    for index, line in enumerate(lines):
        if method_at is not None and index == method_at:
            continue
        stripped = line.strip()
        in_method = method_at is not None and index > method_at
        if not in_method and stripped and _BULLET.match(stripped):
            cleaned = _strip_marker(stripped)
            if cleaned:
                ingredients.append(cleaned)
            continue
        if in_method or method_at is None:
            method_lines.append(line)
    return ingredients, _steps_from(method_lines)


def _ingredient_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        text = _strip_marker(line)
        if text:
            cleaned.append(text)
    return cleaned


def _strip_marker(line: str) -> str:
    text = line.strip()
    while True:
        stripped = _BULLET.sub("", text, count=1).strip()
        if stripped != text:
            text = stripped
            continue
        stripped = _LIST_NUM.sub("", text, count=1).strip()
        if stripped != text:
            text = stripped
            continue
        return text


def _starts_with_marker(line: str) -> bool:
    text = line.strip()
    return bool(_BULLET.match(text) or _LIST_NUM.match(text))


def _blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _steps_from(lines: list[str]) -> list[str]:
    if any(_starts_with_marker(line) for line in lines if line.strip()):
        return _marked_steps(lines)
    steps: list[str] = []
    for block in _blocks(lines):
        cleaned = [_strip_marker(line) for line in block]
        cleaned = [line for line in cleaned if line]
        if not cleaned:
            continue
        # Each line already ends a sentence, so the line break was a new step.
        # A line that does not is a wrap of the one above it.
        if len(cleaned) > 1 and all(line.endswith(_SENTENCE_END) for line in cleaned):
            steps.extend(cleaned)
        else:
            steps.append(" ".join(cleaned))
    return steps


def _marked_steps(lines: list[str]) -> list[str]:
    steps: list[str] = []
    current: list[str] = []
    broke = False

    def flush() -> None:
        text = " ".join(_strip_marker(part) for part in current if part.strip())
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            steps.append(text)
        current.clear()

    for line in lines:
        if not line.strip():
            broke = bool(current)
            continue
        if current and (broke or _starts_with_marker(line)):
            flush()
        broke = False
        current.append(line)
    flush()
    return steps
