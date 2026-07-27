"""The pre-shop audit: decide the few things actually worth walking to check.

This is the point of SousChef. Auditing a whole pantry is the drudgery the app
exists to delete, so the question is never "what do we not know?" -- it is
almost everything -- but:

    Which uncertainties would change what I buy?

An item earns a place on the checklist only if all three hold:

1. **It's in play.** Something this week needs it, or it's near its restock
   floor. Uncertainty about a thing you don't need is free.
2. **It's near the decision boundary.** Its plausible range straddles "enough"
   and "not enough". If even the pessimistic estimate covers the requirement,
   don't buy and don't ask; if even the optimistic one falls short, buy without
   asking. Only a straddling range makes checking worthwhile.
3. **We're unsure.** A level confirmed yesterday needs no re-check.

The score is expected value of information: probability the belief is wrong in a
way that flips the decision, times the cost of getting it wrong. Both failure
directions are real but asymmetric -- a needless duplicate is mildly annoying,
while a missing ingredient can wreck a planned meal and force a second trip,
which is exactly what the user is trying to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from souschef.config import Settings, get_settings
from souschef.core.estimation import estimated_bounds
from souschef.core.inventory import PantryView, pantry_view
from souschef.models import CanonicalIngredient

# Being short an ingredient costs more than buying a spare: one wrecks a meal or
# forces a second trip, the other wastes a few dollars and some shelf space.
COST_OF_SHORTFALL = 3.0
COST_OF_DUPLICATE = 1.0


class AuditReason(StrEnum):
    NEEDED_THIS_WEEK = "needed_this_week"
    NEAR_RESTOCK = "near_restock"


@dataclass(frozen=True)
class AuditCandidate:
    ingredient: CanonicalIngredient
    required: float  # base units needed by the plan (0 if only a restock check)
    threshold: float  # the level the decision actually turns on
    believed: float  # what we think is there
    confidence: float
    low: float  # pessimistic bound
    high: float  # optimistic bound
    reason: AuditReason
    score: float

    @property
    def straddles_decision(self) -> bool:
        """True when the plausible range spans both sides of the decision.

        Tests ``threshold``, not ``required``: for a restock check the boundary
        is the restock floor and ``required`` is zero, so testing ``required``
        would silently disqualify every restock candidate.
        """
        return self.low < self.threshold < self.high

    @property
    def prompt(self) -> str:
        """What to actually ask, phrased as the physical check to perform."""
        name = self.ingredient.name
        if self.required > 0:
            unit = self.ingredient.base_unit
            return f"How much {name} do you have? (need about {self.required:.0f} {unit})"
        return f"Roughly how much {name} is left?"


def _probability_wrong(candidate_low: float, candidate_high: float, required: float) -> float:
    """Fraction of the plausible range that sits on the wrong side of the decision.

    Treats the belief as uniform over ``[low, high]`` -- deliberately crude, but
    the ranking only needs to be monotone in "how close is this to the line",
    and a uniform assumption is honest about how little is known. A sharper
    distribution would imply a precision the underlying data doesn't have.
    """
    span = candidate_high - candidate_low
    if span <= 0:
        return 0.0
    if required <= candidate_low or required >= candidate_high:
        return 0.0
    below = (required - candidate_low) / span
    return min(below, 1.0 - below) * 2.0


def score_candidate(
    view: PantryView,
    required: float,
    reason: AuditReason,
) -> AuditCandidate:
    low, high = estimated_bounds(view.quantity, view.confidence)

    if required > 0:
        threshold = required
        # Which way would an error hurt? If we believe we have enough, the risk
        # is a shortfall; if we believe we don't, the risk is a needless buy.
        cost = COST_OF_SHORTFALL if view.quantity >= required else COST_OF_DUPLICATE
    else:
        threshold = view.item.restock_threshold or 0.0
        cost = COST_OF_DUPLICATE

    p_wrong = _probability_wrong(low, high, threshold)

    # Staples are cheap to over-buy and rarely ruin a meal, so they should not
    # crowd out the questions that matter.
    if view.is_staple:
        cost *= 0.4

    return AuditCandidate(
        ingredient=view.ingredient,
        required=required,
        threshold=threshold,
        believed=view.quantity,
        confidence=view.confidence,
        low=low,
        high=high,
        reason=reason,
        score=round(p_wrong * cost, 6),
    )


def build_audit(
    session: Session,
    user_id: int,
    requirements: dict[int, float],
    *,
    settings: Settings | None = None,
    limit: int | None = None,
) -> list[AuditCandidate]:
    """Rank the physical checks worth doing before finalizing a shopping list.

    ``requirements`` maps ingredient id to the amount the plan needs, in base
    units. Returns at most ``audit_max_items`` -- a checklist long enough to be
    ignored has failed at its job.
    """
    settings = settings or get_settings()
    limit = limit if limit is not None else settings.audit_max_items

    views = {
        v.ingredient.id: v for v in pantry_view(session, user_id, include_empty=True)
    }
    candidates: list[AuditCandidate] = []

    for ingredient_id, required in requirements.items():
        view = views.get(ingredient_id)
        if view is None:
            # Nothing on record: there is no belief to verify, so it simply
            # goes on the list. Asking "do you have any?" when we have never
            # seen it is the kind of busywork this feature exists to avoid.
            continue
        if view.confidence >= settings.audit_confidence_threshold:
            continue
        candidate = score_candidate(view, required, AuditReason.NEEDED_THIS_WEEK)
        if candidate.straddles_decision and candidate.score > 0:
            candidates.append(candidate)

    # Restock checks for things not in this week's plan but near their floor.
    for ingredient_id, view in views.items():
        if ingredient_id in requirements:
            continue
        threshold = view.item.restock_threshold
        if not threshold or view.confidence >= settings.audit_confidence_threshold:
            continue
        candidate = score_candidate(view, 0.0, AuditReason.NEAR_RESTOCK)
        if candidate.straddles_decision and candidate.score > 0:
            candidates.append(candidate)

    candidates.sort(key=lambda c: (c.score, -c.confidence), reverse=True)
    return candidates[:limit]
