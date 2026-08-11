"""Confidence decay for pantry levels.

The premise of the app is that the inventory is *wrong* and knows it. What
matters is not the number but how much to trust it, because that is what decides
whether you need to walk to the cupboard before shopping.

Two things erode trust:

1. **Time.** You use things without telling the app. A level confirmed this
   morning is worth more than one confirmed five weeks ago.
2. **Cooking.** Every cook event subtracts an *estimate* -- recipes say "a
   drizzle of oil", pans get an extra glug -- so error compounds per event
   rather than per day.

Bulk staples decay faster than countable items: you know you have four eggs,
but nobody knows how much rice is in the jar.

Confidence is never used to change a quantity, only to decide whether to ask.
Silently adjusting numbers would make the app confidently wrong; asking is
cheap and keeps the user in control.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from souschef.config import Settings, get_settings
from souschef.models import PantryItem, TrackingMode

# Never claim more than this from decay alone -- an unconfirmed level is never
# certain no matter how recently it was touched.
MAX_CONFIDENCE = 1.0
# Floor: below this the estimate carries no information, so there is nothing to
# gain by decaying it further.
MIN_CONFIDENCE = 0.05


@dataclass(frozen=True)
class DecayInputs:
    tracking_mode: TrackingMode
    last_confirmed_at: datetime
    cooks_since_confirmed: int
    base_confidence: float = 1.0


def _half_life_days(mode: TrackingMode, settings: Settings) -> float:
    if mode == TrackingMode.DISCRETE:
        return settings.confidence_half_life_discrete_days
    return settings.confidence_half_life_bulk_days


def current_confidence(
    inputs: DecayInputs,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> float:
    """How much the stored level deserves to be trusted right now.

    Exponential decay on elapsed time, plus a fixed penalty per cook event since
    the last physical confirmation.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    anchor = inputs.last_confirmed_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)

    elapsed_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
    half_life = _half_life_days(inputs.tracking_mode, settings)

    time_factor = 0.5 ** (elapsed_days / half_life) if half_life > 0 else 0.0
    cook_penalty = settings.confidence_penalty_per_cook * max(0, inputs.cooks_since_confirmed)

    confidence = inputs.base_confidence * time_factor - cook_penalty
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, confidence))


def confidence_for_item(
    item: PantryItem,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> float:
    return current_confidence(
        DecayInputs(
            tracking_mode=item.ingredient.tracking_mode,
            last_confirmed_at=item.last_confirmed_at,
            cooks_since_confirmed=item.cooks_since_confirmed,
            base_confidence=item.confidence,
        ),
        now=now,
        settings=settings,
    )


def days_until_confidence(
    mode: TrackingMode,
    target: float,
    *,
    from_confidence: float = 1.0,
    settings: Settings | None = None,
) -> float:
    """When a level will decay to ``target``. Used to schedule re-checks."""
    settings = settings or get_settings()
    if target <= 0 or from_confidence <= 0 or target >= from_confidence:
        return 0.0
    half_life = _half_life_days(mode, settings)
    return half_life * math.log2(from_confidence / target)


def estimated_bounds(quantity: float, confidence: float) -> tuple[float, float]:
    """A plausible range for a level, widening as confidence falls.

    Used by the audit to decide whether a level is near a decision boundary. At
    full confidence the range collapses to the point estimate; at the floor it
    spans roughly nothing-to-double, which is the honest description of a number
    nobody has checked in two months.
    """
    spread = (1.0 - max(0.0, min(1.0, confidence))) * quantity
    return max(0.0, quantity - spread), quantity + spread
