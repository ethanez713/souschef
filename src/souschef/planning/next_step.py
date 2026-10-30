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
        return NextStep(label, "/shopping#check-first", "search")
    if state.to_get > 0:
        return NextStep(f"Shop: {state.to_get} to get", "/shopping", "shopping-basket")
    if state.in_basket > 0:
        return NextStep(f"Add {state.in_basket} to the pantry", "/shopping", "refrigerator")
    return NextStep("You're set", None, "circle-check")


def _named(names: Sequence[str]) -> str:
    shown = list(names[:3])
    extra = len(names) - len(shown)
    text = ", ".join(shown)
    if extra:
        text += f" +{extra} more"
    return text


def readiness_line(
    missing: Sequence[str],
    unidentified: int = 0,
    to_check: Sequence[str] = (),
) -> Readiness:
    """How close a scored recipe is to being cookable.

    ``missing`` and ``to_check`` are ingredient names in recipe order. At most
    three are written out; the rest collapse to "+N more". Lines that never
    matched a catalog ingredient are a separate clause, since they have no
    name to list. A recipe whose only gaps are checks is not ready.
    """
    parts: list[str] = []
    tone = "good"
    if missing:
        parts.append(f"Missing {len(missing)}: {_named(missing)}")
        tone = "warn"
    if to_check:
        parts.append(f"Check {len(to_check)}: {_named(to_check)}")
        tone = "warn"
    text = " · ".join(parts) if parts else "Ready to cook"
    if unidentified > 0:
        text += f" · {unidentified} needs naming"
    return Readiness(text, tone)


def readiness_chip(suggestion: object) -> str:
    """Ready, Missing N, Check N, or Missing N · check N."""
    missing_n = len(getattr(suggestion, "missing", ()) or ()) + int(
        getattr(suggestion, "unidentified", 0) or 0
    )
    check_n = len(getattr(suggestion, "to_check", ()) or ())
    if missing_n == 0 and check_n == 0:
        return "Ready"
    if missing_n and check_n:
        return f"Missing {missing_n} · check {check_n}"
    if check_n:
        return f"Check {check_n}"
    return f"Missing {missing_n}"


def recipe_readiness(suggestion: object) -> str:
    """The recipes-list sub-line: the gaps by name. The row's chip already gives the count."""
    names = [gap.ingredient.name for gap in getattr(suggestion, "missing", ()) or ()]
    checks = [item.name for item in getattr(suggestion, "to_check", ()) or ()]
    unidentified = int(getattr(suggestion, "unidentified", 0) or 0)
    clauses = []
    if names:
        clauses.append(_named(names))
    if checks:
        clauses.append("check " + _named(checks))
    if unidentified:
        clauses.append(f"{unidentified} needs naming")
    return " · ".join(clauses)
