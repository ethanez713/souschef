"""Extract schema.org Recipe data from a page's JSON-LD.

Nearly every recipe site emits schema.org Recipe markup because it drives search
result cards, which makes JSON-LD both the most reliable and the most stable
extraction target -- far better than CSS selectors that change with every
redesign.

This module is pure parsing over an HTML string. It performs no I/O, so it can
be tested against saved fixtures without network access.
"""

from __future__ import annotations

import contextlib
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

_ISO_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?)?"
)


@dataclass
class RawRecipe:
    """A recipe as the source described it, before any of our interpretation."""

    title: str
    ingredients: list[str] = field(default_factory=list)
    instructions: str | None = None
    servings: int | None = None
    total_minutes: int | None = None
    image_url: str | None = None
    author: str | None = None
    description: str | None = None
    source_url: str | None = None
    external_id: str | None = None

    @property
    def is_usable(self) -> bool:
        """A recipe with no ingredients is not a recipe, whatever the markup said."""
        return bool(self.title and self.ingredients)


class _JsonLdCollector(HTMLParser):
    """Pull the contents of every <script type="application/ld+json"> block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._capturing = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr = {k.lower(): (v or "").lower() for k, v in attrs}
        if "ld+json" in attr.get("type", ""):
            self._capturing = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.blocks.append("".join(self._buffer))
            self._capturing = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)


def _iter_nodes(payload: Any) -> list[dict]:
    """Flatten the shapes JSON-LD arrives in: object, list, or @graph."""
    nodes: list[dict] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            nodes.append(node)
            if "@graph" in node:
                stack.append(node["@graph"])
    return nodes


def _is_recipe(node: dict) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return any(str(t).lower() == "recipe" for t in node_type)
    return str(node_type or "").lower() == "recipe"


def unescape_text(value: str | None) -> str | None:
    """Turn ``3&quot;`` into a real quote. Repeat so ``&amp;quot;`` finishes too."""
    if value is None or "&" not in value:
        return value
    current = value
    for _ in range(2):
        decoded = html.unescape(current)
        if decoded == current:
            break
        current = decoded
    return current


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = unescape_text(value.strip())
        return text or None
    if isinstance(value, dict):
        return _as_text(value.get("name") or value.get("text") or value.get("@value"))
    if isinstance(value, list):
        parts = [t for t in (_as_text(v) for v in value) if t]
        return "\n".join(parts) or None
    return str(value)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = _as_text(item)
            if text:
                out.append(text)
        return out
    text = _as_text(value)
    return [text] if text else []


def _parse_instructions(value: Any) -> str | None:
    """Flatten schema.org's several instruction shapes into readable steps."""
    if value is None:
        return None
    if isinstance(value, str):
        return _as_text(value)

    steps: list[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str):
            text = _as_text(item)
            if text:
                steps.append(text)
        elif isinstance(item, dict):
            item_type = str(item.get("@type", "")).lower()
            if item_type == "howtosection":
                nested = _parse_instructions(item.get("itemListElement"))
                if nested:
                    steps.append(nested)
            else:
                text = _as_text(item.get("text") or item.get("name"))
                if text:
                    steps.append(text)
    return "\n\n".join(steps) or None


def _parse_servings(value: Any) -> int | None:
    text = _as_text(value)
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _parse_duration(value: Any) -> int | None:
    """ISO-8601 duration to minutes."""
    text = _as_text(value)
    if not text:
        return None
    match = _ISO_DURATION.fullmatch(text.strip())
    if not match:
        digits = re.search(r"\d+", text)
        return int(digits.group()) if digits else None
    parts = match.groupdict()
    minutes = int(parts.get("days") or 0) * 1440
    minutes += int(parts.get("hours") or 0) * 60
    minutes += int(parts.get("minutes") or 0)
    return minutes or None


def _parse_image(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return _parse_image(value[0])
    if isinstance(value, dict):
        return _as_text(value.get("url") or value.get("contentUrl"))
    return _as_text(value)


def page_key(url: str | None) -> tuple[str, str] | None:
    """Host and path, ignoring scheme, a leading www, and a trailing slash.

    Query strings are dropped on purpose: a recipe's canonical URL and the
    address in the bar often differ only by tracking parameters.
    """
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    return host, parts.path.rstrip("/") or "/"


def extract_recipe(html: str, *, source_url: str | None = None) -> RawRecipe | None:
    """Find the schema.org Recipe for this page. None if absent.

    Pages embed related recipes beside the one you asked for. Prefer the node
    whose own URL is the page that was fetched; otherwise keep the first usable
    recipe, which is what a page with a single recipe and no URL still is.
    """
    collector = _JsonLdCollector()
    # Malformed markup must not raise: whatever blocks were collected before the
    # parser choked are still worth trying.
    with contextlib.suppress(Exception):
        collector.feed(html)

    found: list[tuple[str | None, RawRecipe]] = []
    for block in collector.blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue

        for node in _iter_nodes(payload):
            if not _is_recipe(node):
                continue

            own_url = _as_text(node.get("url"))
            recipe = RawRecipe(
                title=_as_text(node.get("name")) or "Untitled recipe",
                ingredients=_as_list(node.get("recipeIngredient") or node.get("ingredients")),
                instructions=_parse_instructions(node.get("recipeInstructions")),
                servings=_parse_servings(node.get("recipeYield")),
                total_minutes=(
                    _parse_duration(node.get("totalTime"))
                    or _parse_duration(node.get("cookTime"))
                ),
                image_url=_parse_image(node.get("image")),
                author=_as_text(node.get("author")),
                description=_as_text(node.get("description")),
                source_url=own_url or source_url,
            )
            if recipe.is_usable:
                found.append((own_url, recipe))

    if not found:
        return None

    wanted = page_key(source_url)
    if wanted is not None:
        for own_url, recipe in found:
            if page_key(own_url) == wanted:
                return recipe
    return found[0][1]
