"""The belief fold: levels as a best estimate inside an absolute range."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from souschef.core.belief import Belief, BeliefEvent, drift, step
from souschef.models import EventReason

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DISCRETE_HALF_LIFE = 45.0
BULK_HALF_LIFE = 18.0


def event(
    reason: EventReason,
    delta: float = 0.0,
    *,
    level: float = 0.0,
    u: float = 0.0,
    at: datetime = T0,
) -> BeliefEvent:
    return BeliefEvent(reason, delta, level, u, at)


def fold(belief: Belief, ev: BeliefEvent, half_life: float = BULK_HALF_LIFE) -> Belief:
    return step(belief, ev, half_life_days=half_life, cook_estimate_error=0.25)


def test_a_purchase_adds_a_known_amount_to_every_bound() -> None:
    observed = fold(Belief(), event(EventReason.CONFIRM, level=0, at=T0 - timedelta(days=60)))
    bought = fold(observed, event(EventReason.PURCHASE, 907, level=907))

    assert (bought.quantity, bought.low, bought.high) == (907, 907, 907)
    assert bought.certainty == 1.0


def test_unlogged_use_lowers_only_the_low_bound() -> None:
    belief = fold(Belief(), event(EventReason.CONFIRM, level=500))
    later = drift(belief, T0 + timedelta(days=18), BULK_HALF_LIFE)

    assert (later.quantity, later.low, later.high) == (500, pytest.approx(250), 500)


def test_a_cook_widens_the_range_by_the_estimate_error() -> None:
    belief = Belief(500, 400, 600, belief_at=T0)
    cooked = fold(belief, event(EventReason.COOK, -300))

    assert (cooked.quantity, cooked.low, cooked.high) == (200, 25, 375)


def test_cooking_more_than_the_range_allows_proves_the_stock_existed() -> None:
    belief = Belief(100, 0, 200, belief_at=T0)

    proved = fold(belief, event(EventReason.COOK, -500))
    assert (proved.quantity, proved.low, proved.high) == (0, 0, 500)
    assert proved.certainty == 0

    clamped = fold(belief, event(EventReason.COOK, -150))
    assert (clamped.quantity, clamped.low, clamped.high) == (0, 0, 87.5)


def test_throwing_out_the_last_of_it_is_an_observation() -> None:
    belief = Belief(300, 100, 400, belief_at=T0)
    binned = fold(belief, event(EventReason.WASTE, -300, at=T0 + timedelta(days=1)))

    assert (binned.quantity, binned.low, binned.high) == (0, 0, 0)
    assert binned.last_confirmed_at == T0 + timedelta(days=1)


def test_a_confirmation_collapses_the_range_to_the_observed_level() -> None:
    belief = Belief(100, 0, 500, belief_at=T0)
    confirmed = fold(belief, event(EventReason.CONFIRM, level=100, at=T0 + timedelta(days=3)))

    assert (confirmed.quantity, confirmed.low, confirmed.high) == (100, 100, 100)


def test_an_observation_with_uncertainty_keeps_a_range() -> None:
    counted = fold(Belief(), event(EventReason.MANUAL, level=100, u=20))

    assert (counted.low, counted.quantity, counted.high) == (80, 100, 120)


def test_drift_is_a_no_op_before_the_first_event() -> None:
    assert drift(Belief(), T0, DISCRETE_HALF_LIFE) == Belief()
