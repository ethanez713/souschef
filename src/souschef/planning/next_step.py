"""The one next action on Home. First match wins."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class NextStepState:
    recipe_count: int
    planned_ahead: int
    check_count: int
    to_get: int
    in_basket: int


@dataclass(frozen=True)
class NextStep:
    label: str
    href: str | None
    icon: str


@dataclass(frozen=True)
class Readiness:
    text: str
    tone: str


def next_step(state: NextStepState) -> NextStep:
    if state.recipe_count <= 0:
        return NextStep("Add your first recipe", "/recipes?import=1", "book-open")
    if state.planned_ahead <= 0:
        return NextStep("Plan this week", "/plan", "calendar-days")
    if state.check_count > 0 and state.to_get > 0:
        noun = "level" if state.check_count == 1 else "levels"
        label = f"Check {state.check_count} {noun} before you shop"
        return NextStep(label, "/audit", "search")
    if state.to_get > 0:
        return NextStep(f"Shop: {state.to_get} to get", "/shopping", "shopping-basket")
    if state.in_basket > 0:
        return NextStep(f"Add {state.in_basket} to the pantry", "/shopping", "refrigerator")
    return NextStep("You're set", None, "circle-check")


def readiness_line(missing: Sequence[str], unidentified: int = 0) -> Readiness:
    """How close a scored recipe is to being cookable.

    ``missing`` is ingredient names in recipe order. At most three are written
    out; the rest collapse to "+N more". Lines that never matched a catalog
    ingredient are a separate clause, since they have no name to list.
    """
    if missing:
        named = list(missing[:3])
        extra = len(missing) - len(named)
        text = f"Missing {len(missing)}: {', '.join(named)}"
        if extra:
            text += f" +{extra} more"
        tone = "warn"
    else:
        text = "Ready to cook"
        tone = "good"
    if unidentified > 0:
        text += f" · {unidentified} needs naming"
    return Readiness(text, tone)


def recipe_readiness(suggestion: object) -> str:
    """The recipes-list sub-line: the gaps by name. The row's chip already gives the count."""
    names = [gap.ingredient.name for gap in getattr(suggestion, "missing", ()) or ()]
    unidentified = int(getattr(suggestion, "unidentified", 0) or 0)
    clauses = []
    if names:
        extra = len(names) - 3
        clauses.append(", ".join(names[:3]) + (f" +{extra} more" if extra > 0 else ""))
    if unidentified:
        clauses.append(f"{unidentified} needs naming")
    return " · ".join(clauses)
