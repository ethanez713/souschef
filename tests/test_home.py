"""Home is tonight plus one next step, and the pure function behind that step."""

from __future__ import annotations

import re
from datetime import date, timedelta
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import record_event
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    MealPlanEntry,
    MealSlot,
    RecipeSource,
    User,
)
from souschef.planning.next_step import NextStepState, next_step, readiness_line

_ICONS = set(
    re.findall(
        r"'([a-z0-9-]+)':",
        (
            Path(__file__).resolve().parents[1] / "src/souschef/web/templates/icons.html"
        ).read_text(),
    )
)


def _state(
    recipe_count: int,
    planned_ahead: int,
    check_count: int = 0,
    to_get: int = 0,
    in_basket: int = 0,
) -> NextStepState:
    return NextStepState(recipe_count, planned_ahead, check_count, to_get, in_basket)


@pytest.mark.parametrize(
    ("state", "label", "href", "icon"),
    [
        pytest.param(
            _state(0, 3, 2, 4, 1),
            "Add your first recipe",
            "/recipes?import=1",
            "book-open",
            id="no recipes wins over a plan and a list",
        ),
        pytest.param(
            _state(2, 0, 2, 4, 1),
            "Plan this week",
            "/plan",
            "calendar-days",
            id="nothing planned wins over check, shop, and basket",
        ),
        pytest.param(
            _state(2, 1, 1, 4, 3),
            "Check 1 level before you shop",
            "/shopping#check-first",
            "search",
            id="one level to check wins over shop and basket",
        ),
        pytest.param(
            _state(2, 1, 4, 2, 9),
            "Check 4 levels before you shop",
            "/shopping#check-first",
            "search",
            id="several levels to check",
        ),
        pytest.param(
            _state(2, 1, 0, 1, 9),
            "Shop: 1 to get",
            "/shopping",
            "shopping-basket",
            id="to get wins over the basket",
        ),
        pytest.param(
            _state(2, 1, 0, 6, 0),
            "Shop: 6 to get",
            "/shopping",
            "shopping-basket",
            id="shop when nothing is in the basket",
        ),
        pytest.param(
            _state(2, 1, 5, 0, 1),
            "Add 1 to the pantry",
            "/shopping",
            "refrigerator",
            id="basket wins when there is nothing left to get",
        ),
        pytest.param(
            _state(2, 1, 0, 0, 3),
            "Add 3 to the pantry",
            "/shopping",
            "refrigerator",
            id="several ticked rows",
        ),
        pytest.param(
            _state(2, 1, 3, 0, 0),
            "You're set",
            None,
            "circle-check",
            id="a check with nothing to buy is not the next step",
        ),
        pytest.param(
            _state(4, 2, 0, 0, 0),
            "You're set",
            None,
            "circle-check",
            id="planned and nothing outstanding",
        ),
    ],
)
def test_next_step_first_match(
    state: NextStepState, label: str, href: str | None, icon: str
) -> None:
    step = next_step(state)
    assert (step.label, step.href, step.icon) == (label, href, icon)
    assert icon in _ICONS


@pytest.mark.parametrize(
    ("missing", "unidentified", "to_check", "text", "tone"),
    [
        ([], 0, [], "Ready to cook", "good"),
        ([], 1, [], "Ready to cook · 1 needs naming", "good"),
        ([], 2, [], "Ready to cook · 2 needs naming", "good"),
        (["garlic"], 0, [], "Missing 1: garlic", "warn"),
        (["garlic", "onion"], 1, [], "Missing 2: garlic, onion · 1 needs naming", "warn"),
        (["a", "b", "c"], 0, [], "Missing 3: a, b, c", "warn"),
        (["a", "b", "c", "d"], 0, [], "Missing 4: a, b, c +1 more", "warn"),
        (
            ["a", "b", "c", "d", "e"],
            2,
            [],
            "Missing 5: a, b, c +2 more · 2 needs naming",
            "warn",
        ),
        ([], 0, ["rice", "cream"], "Check 2: rice, cream", "warn"),
        (["garlic"], 0, ["rice"], "Missing 1: garlic · Check 1: rice", "warn"),
    ],
)
def test_readiness_line_names_up_to_three(
    missing: list[str], unidentified: int, to_check: list[str], text: str, tone: str
) -> None:
    got = readiness_line(missing, unidentified, to_check)
    assert (got.text, got.tone) == (text, tone)


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None
    return found


def _recipe(session: Session, user: User, title: str, ingredients: list[str]) -> object:
    return save_imported_recipe(
        session,
        user.id,
        RawRecipe(
            title=title,
            ingredients=ingredients,
            servings=2,
            source_url=f"https://example.com/{title.replace(' ', '-').lower()}",
        ),
        source=RecipeSource.URL,
    )


def _plan(
    session: Session,
    user: User,
    recipe,  # noqa: ANN001
    day: date,
    *,
    title: str | None = None,
) -> MealPlanEntry:
    entry = MealPlanEntry(
        user_id=user.id,
        plan_date=day,
        slot=MealSlot.DINNER,
        recipe_id=recipe.id if recipe is not None else None,
        freeform_title=title,
        servings=1,
    )
    session.add(entry)
    session.flush()
    return entry


def _cook_anchors(html: str) -> list[tuple[str, str]]:
    return re.findall(r'href="([^"]*?/cook[^"]*)"[^>]*>\s*([^<]*?)\s*</a>', html)


def _no_old_home(html: str) -> None:
    assert "Missing 1–3" not in html
    assert "Missing 4+" not in html
    assert "<span>Ready</span>" not in html
    assert "<span>Planned</span>" not in html
    assert 'action="/pantry/' not in html
    assert "<h1>Tonight</h1>" not in html


def test_a_planned_recipe_shows_one_cook_link_and_what_is_missing(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _recipe(seeded_session, web_user, "Tonight Pasta", ["1 pound spaghetti"])
    entry = _plan(seeded_session, web_user, recipe, date.today())
    seeded_session.commit()

    page = signed_in.get("/").text

    assert "<h1>Home</h1>" in page
    assert date.today().strftime("%A %-d %B") in page
    assert _cook_anchors(page) == [(f"/recipes/{recipe.id}/cook?entry={entry.id}", "Cook")]
    assert "Check 1: spaghetti" in page
    assert 'class="tonight-ready warn"' in page
    assert page.count("Tonight Pasta") == 1
    assert "Cook this" not in page
    assert 'href="/plan">Change</a>' in page
    assert "Nothing planned tonight" not in page
    assert "Tomorrow ·" not in page
    _no_old_home(page)

    shop = signed_in.get("/shopping").text
    expected = _step_from_shopping(shop)
    assert expected is not None
    assert f'class="list-row-title">{expected}</span>' in unescape(page)


def test_nothing_planned_says_so(
    signed_in: TestClient,
) -> None:
    page = signed_in.get("/").text
    assert "Nothing planned tonight" in page
    assert _cook_anchors(page) == []
    assert "Cook this" not in page
    assert 'href="/plan">Plan something</a>' in page
    assert "Tomorrow ·" not in page
    assert 'class="list-row home-step" href="/recipes?import=1"' in page
    _no_old_home(page)


def test_nothing_planned_offers_the_best_nearly_ready_recipe(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _recipe(seeded_session, web_user, "Almost Pasta", ["1 pound spaghetti"])
    seeded_session.commit()

    page = signed_in.get("/").text

    assert "Nothing planned tonight" in page
    assert _cook_anchors(page) == [(f"/recipes/{recipe.id}/cook", "Cook this")]
    assert "Check 1: spaghetti" in page
    assert "Plan something" in page
    assert "Tomorrow ·" not in page
    _no_old_home(page)


def test_nothing_planned_offers_a_recipe_that_is_ready(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _recipe(seeded_session, web_user, "Ready Pasta", ["1 pound spaghetti"])
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "spaghetti"),
        reason=EventReason.PURCHASE, delta=2000,
    )
    seeded_session.commit()

    page = signed_in.get("/").text

    assert "Nothing planned tonight" in page
    assert _cook_anchors(page) == [(f"/recipes/{recipe.id}/cook", "Cook this")]
    assert "Ready to cook" in page
    assert 'class="list-row good"' in page
    _no_old_home(page)


def test_a_recipe_that_needs_a_real_shop_is_not_offered(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _recipe(
        seeded_session,
        web_user,
        "Far From Ready",
        [
            "1 pound spaghetti",
            "3 cloves garlic",
            "1 onion",
            "2 tbsp olive oil",
            "1 pound chicken breast",
        ],
    )
    for line in recipe.ingredients:
        ingredient = line.ingredient
        if ingredient is not None and not ingredient.is_staple:
            record_event(
                seeded_session, web_user.id, ingredient, reason=EventReason.MANUAL, level=0
            )
    seeded_session.commit()

    page = signed_in.get("/").text

    assert "Nothing planned tonight" in page
    assert "Cook this" not in page
    assert "Far From Ready" not in page
    assert "Plan something" in page
    _no_old_home(page)


def test_a_freeform_night_has_no_cook_link(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _recipe(seeded_session, web_user, "Ready Pasta", ["1 pound spaghetti"])
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "spaghetti"),
        reason=EventReason.PURCHASE, delta=2000,
    )
    _plan(seeded_session, web_user, None, date.today(), title="Eating out")
    seeded_session.commit()

    page = signed_in.get("/").text

    assert "Eating out" in page
    assert _cook_anchors(page) == []
    assert "Cook this" not in page
    assert recipe.title not in page
    assert 'href="/plan">Change</a>' in page
    assert "Nothing planned tonight" not in page
    assert "You&#39;re set" in page
    assert 'class="list-row home-step" href=' not in page
    _no_old_home(page)


def test_tomorrow_is_one_row_and_only_when_it_is_planned(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    _plan(seeded_session, web_user, None, date.today() + timedelta(days=1), title="Leftovers")
    seeded_session.commit()

    page = signed_in.get("/").text

    assert "Tomorrow · Leftovers" in page
    assert 'href="/plan"' in page
    assert "Nothing planned tonight" in page
    assert _cook_anchors(page) == []


def test_an_unidentified_line_stays_on_the_readiness(
    signed_in: TestClient, seeded_session: Session, web_user: User
) -> None:
    recipe = _recipe(
        seeded_session,
        web_user,
        "Named Pasta",
        ["1 pound spaghetti", "1 cup xyzzybroth"],
    )
    record_event(
        seeded_session, web_user.id, ing(seeded_session, "spaghetti"),
        reason=EventReason.PURCHASE, delta=2000,
    )
    entry = _plan(seeded_session, web_user, recipe, date.today())
    seeded_session.commit()

    page = signed_in.get("/").text

    assert _cook_anchors(page) == [(f"/recipes/{recipe.id}/cook?entry={entry.id}", "Cook")]
    assert "Ready to cook · 1 needs naming" in page
    _no_old_home(page)


def _step_from_shopping(shop_html: str) -> str | None:
    """The next-step label implied by the figures the shopping page itself renders."""
    counted = re.search(r'class="num">(\d+)</span> to get', shop_html)
    if counted is None:
        return None
    to_get = int(counted.group(1))
    audit = re.search(r"Check first \((\d+)\)", shop_html)
    if audit and to_get:
        n = int(audit.group(1))
        noun = "level" if n == 1 else "levels"
        return f"Check {n} {noun} before you shop"
    if to_get:
        return f"Shop: {to_get} to get"
    basket = re.search(r"Add (\d+) ticked", shop_html)
    if basket:
        return f"Add {int(basket.group(1))} to the pantry"
    return "You're set"
