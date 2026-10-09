"""A pantry level as a best estimate inside an absolute range, folded from the ledger.

The app's premise is that the inventory may be wrong and knows how wrong. A belief is
``quantity`` with the range ``[low, high]`` it could plausibly sit in, all in base
units. Only the folding rules live here; they are pure, so replaying any history
gives the same belief as building it event by event.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from souschef.models.inventory import EventReason

OBSERVATIONS = frozenset({EventReason.CONFIRM, EventReason.MANUAL, EventReason.INITIAL})


@dataclass(frozen=True)
class Belief:
    quantity: float = 0.0
    low: float = 0.0
    high: float = 0.0
    belief_at: datetime | None = None
    last_confirmed_at: datetime | None = None

    @property
    def certainty(self) -> float:
        return self.low / self.high if self.high > 0 else 1.0


@dataclass(frozen=True)
class BeliefEvent:
    reason: EventReason
    delta: float
    resulting_quantity: float
    uncertainty: float
    occurred_at: datetime


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def drift(belief: Belief, now: datetime, half_life_days: float) -> Belief:
    """Unlogged use only ever lowers a level, so only ``low`` decays."""
    if belief.belief_at is None:
        return belief
    days = max(0.0, (aware(now) - aware(belief.belief_at)).total_seconds() / 86400.0)
    factor = 0.5 ** (days / half_life_days) if half_life_days > 0 else 0.0
    return _clamped(replace(belief, low=belief.low * factor))


def _clamped(belief: Belief) -> Belief:
    quantity = max(0.0, belief.quantity)
    high = max(belief.high, quantity)
    low = min(max(0.0, belief.low), quantity)
    return replace(belief, quantity=quantity, low=low, high=high)


def step(
    belief: Belief, event: BeliefEvent, *, half_life_days: float, cook_estimate_error: float
) -> Belief:
    belief = drift(belief, event.occurred_at, half_life_days)
    quantity, low, high = belief.quantity, belief.low, belief.high
    delta, uncertainty = event.delta, event.uncertainty
    confirmed = belief.last_confirmed_at

    if event.reason in OBSERVATIONS:
        quantity = event.resulting_quantity
        low, high = max(0.0, quantity - uncertainty), quantity + uncertainty
        confirmed = event.occurred_at
    elif event.reason == EventReason.PURCHASE:
        quantity, low, high = (
            quantity + delta,
            low + delta - uncertainty,
            high + delta + uncertainty,
        )
    elif event.reason == EventReason.COOK:
        used = -delta
        if used * (1 - cook_estimate_error) > high:
            # Cooking proves the stock existed, so the belief that it was gone was wrong.
            quantity, low, high = 0.0, 0.0, used
        else:
            quantity -= used
            low -= used * (1 + cook_estimate_error)
            high -= used * (1 - cook_estimate_error)
    elif event.reason == EventReason.WASTE:
        quantity, low, high = quantity + delta, low + delta, high + delta
        if quantity <= 0:
            quantity = low = high = 0.0
            confirmed = event.occurred_at

    return replace(
        _clamped(Belief(quantity, low, high)),
        belief_at=event.occurred_at,
        last_confirmed_at=confirmed,
    )
