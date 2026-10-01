"""Use-soon and probably-gone nudges from how long ago an item was stocked."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.core.inventory import PantryView
from souschef.models import EventReason, InventoryEvent

USE_SOON_FRACTION = 0.75
EXPIRY_USE_SOON_DAYS = 3


class Freshness(StrEnum):
    USE_SOON = "use soon"
    PROBABLY_GONE = "probably gone"


def _anchor(view: PantryView, last_purchase: dict[int, date]) -> date:
    """When the stock was last known fresh: bought, or physically checked."""
    checked = view.item.last_confirmed_at.date()
    bought = last_purchase.get(view.ingredient.id)
    return max(checked, bought) if bought else checked


def classify(view: PantryView, last_purchase: dict[int, date], today: date) -> Freshness | None:
    if view.is_staple or view.quantity <= 0:
        return None
    expires_on = view.item.expires_on
    if expires_on is not None:
        days_left = (expires_on - today).days
        if days_left < 0:
            return Freshness.PROBABLY_GONE
        return Freshness.USE_SOON if days_left <= EXPIRY_USE_SOON_DAYS else None
    life = view.ingredient.shelf_life_days
    if not life:
        return None
    age = (today - _anchor(view, last_purchase)).days
    if age > life:
        return Freshness.PROBABLY_GONE
    return Freshness.USE_SOON if age >= life * USE_SOON_FRACTION else None


def freshness_by_ingredient(
    session: Session, user_id: int, views: list[PantryView], today: date
) -> dict[int, Freshness]:
    last_purchase = {
        ingredient_id: when.date()
        for ingredient_id, when in session.execute(
            select(InventoryEvent.ingredient_id, func.max(InventoryEvent.occurred_at))
            .where(
                InventoryEvent.user_id == user_id,
                InventoryEvent.reason == EventReason.PURCHASE,
                InventoryEvent.delta > 0,
            )
            .group_by(InventoryEvent.ingredient_id)
        )
    }
    flagged = {}
    for view in views:
        status = classify(view, last_purchase, today)
        if status is not None:
            flagged[view.ingredient.id] = status
    return flagged
