"""No page shows a phrase from the "Never use" column of docs/plans/00-conventions.md."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import get_or_create_item, record_event
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
    RecipeSource,
)

BANNED = [
    "Cook now", "Nearly there", "Bigger shop", "Missing a few things", "to buy",
    "Least certain", "probably gone", "% sure", "staple", "assumed on hand",
    "Stop assuming", "Wrong match", "unidentified", "ingredient line", "not identified",
    "Confirm level", "I cooked this", "About to cook", "Subtract from pantry",
]


class _VisibleText(HTMLParser):
    """Text a reader sees: no tags, no attribute values, nothing inside script or style."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _visible(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


@pytest.fixture
def kitchen(signed_in: TestClient, seeded_session: Session, web_user) -> dict[str, int]:
    def ing(name: str) -> CanonicalIngredient:
        found = seeded_session.scalar(
            select(CanonicalIngredient).where(CanonicalIngredient.name == name)
        )
        assert found is not None, name
        return found

    recipe = save_imported_recipe(
        seeded_session,
        web_user.id,
        RawRecipe(
            title="Vocabulary Stew",
            ingredients=["1 pound chicken thighs", "2 tbsp zzqx paste", "1 cup heavy cream"],
            servings=2,
            source_url="https://example.com/vocabulary-stew",
        ),
        source=RecipeSource.URL,
    )
    seeded_session.add(
        MealPlanEntry(
            user_id=web_user.id, plan_date=date.today(), slot=MealSlot.DINNER,
            recipe_id=recipe.id, servings=2,
        )
    )
    cream = ing("heavy cream")
    stale = datetime.now(UTC) - timedelta(days=40)
    record_event(
        seeded_session, web_user.id, cream, reason=EventReason.PURCHASE, delta=240.0,
        occurred_at=stale,
    )
    get_or_create_item(seeded_session, web_user.id, cream).last_confirmed_at = stale
    receipt = Receipt(
        user_id=web_user.id, image_path="test.jpg", status=ReceiptStatus.NEEDS_REVIEW,
        ocr_confidence=0.6,
    )
    seeded_session.add(receipt)
    seeded_session.flush()
    seeded_session.add(ReceiptLine(receipt_id=receipt.id, position=0, raw_text="MYSTERY 3.99"))
    seeded_session.commit()
    return {"recipe": recipe.id, "receipt": receipt.id}


PAGES = [
    "/", "/recipes", "/plan", "/shopping", "/pantry", "/receipts", "/settings",
    "/recipes/{recipe}", "/recipes/{recipe}/cook", "/receipts/{receipt}",
]


@pytest.mark.parametrize("path", PAGES)
def test_no_page_uses_a_banned_phrase(
    signed_in: TestClient, kitchen: dict[str, int], path: str
) -> None:
    response = signed_in.get(path.format(**kitchen))
    assert response.status_code == 200
    text = _visible(response.text)

    found = [
        phrase for phrase in BANNED
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE)
    ]

    assert found == []
