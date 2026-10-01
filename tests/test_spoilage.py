"""Use-soon and probably-gone nudges, and their effect on suggestions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import pantry_view, record_event
from souschef.core.spoilage import Freshness, freshness_by_ingredient
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    Recipe,
    RecipeIngredient,
    RecipeSource,
)
from souschef.planning.suggest import suggest

TODAY = date(2026, 6, 15)


def _ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None, name
    return found


def _at(days_ago: int) -> datetime:
    return datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC) - timedelta(days=days_ago)


def _item(session: Session, user_id: int, ingredient: CanonicalIngredient):
    return next(v.item for v in pantry_view(session, user_id) if v.ingredient.id == ingredient.id)


def _bought(session: Session, user_id: int, name: str, days_ago: int) -> CanonicalIngredient:
    """Stock an item as if bought and last looked at ``days_ago`` days before TODAY."""
    ingredient = _ing(session, name)
    record_event(
        session, user_id, ingredient, reason=EventReason.PURCHASE, delta=500.0,
        occurred_at=_at(days_ago),
    )
    _item(session, user_id, ingredient).last_confirmed_at = _at(days_ago)
    return ingredient


def _status(session: Session, user_id: int, ingredient) -> Freshness | None:
    views = pantry_view(session, user_id)
    return freshness_by_ingredient(session, user_id, views, TODAY).get(ingredient.id)


def test_age_against_shelf_life_sets_the_nudge(seeded_session: Session, owner: int) -> None:
    chicken = _bought(seeded_session, owner, "chicken breast", days_ago=1)
    assert chicken.shelf_life_days == 3
    assert _status(seeded_session, owner, chicken) is None

    other = _bought(seeded_session, owner, "spinach", days_ago=6)
    assert _status(seeded_session, owner, other) == Freshness.USE_SOON


def test_old_stock_is_probably_gone_and_a_check_clears_it(
    seeded_session: Session, owner: int
) -> None:
    chicken = _bought(seeded_session, owner, "chicken breast", days_ago=21)
    assert _status(seeded_session, owner, chicken) == Freshness.PROBABLY_GONE

    _item(seeded_session, owner, chicken).last_confirmed_at = _at(0)
    assert _status(seeded_session, owner, chicken) is None


def test_a_staple_is_never_flagged(seeded_session: Session, owner: int) -> None:
    salt = _bought(seeded_session, owner, "kosher salt", days_ago=4000)
    salt.is_staple = True
    assert _status(seeded_session, owner, salt) is None


def test_an_explicit_expiry_date_overrides_shelf_life(
    seeded_session: Session, owner: int
) -> None:
    chicken = _bought(seeded_session, owner, "chicken breast", days_ago=0)
    item = _item(seeded_session, owner, chicken)
    item.expires_on = TODAY - timedelta(days=1)
    assert _status(seeded_session, owner, chicken) == Freshness.PROBABLY_GONE
    item.expires_on = TODAY + timedelta(days=2)
    assert _status(seeded_session, owner, chicken) == Freshness.USE_SOON


def test_a_recipe_does_not_count_spoiled_stock_as_on_hand(
    seeded_session: Session, owner: int
) -> None:
    chicken = _bought(seeded_session, owner, "chicken breast", days_ago=21)
    recipe = Recipe(user_id=owner, title="Old Chicken", source=RecipeSource.MANUAL, servings=1)
    seeded_session.add(recipe)
    seeded_session.flush()
    seeded_session.add(
        RecipeIngredient(
            recipe_id=recipe.id, position=0, raw_text="200 g chicken breast",
            ingredient_id=chicken.id, quantity=200, unit="g",
        )
    )
    seeded_session.flush()

    stale = suggest(seeded_session, owner, today=TODAY)[0]
    assert [m.ingredient.id for m in stale.missing] == [chicken.id]

    _item(seeded_session, owner, chicken).last_confirmed_at = _at(0)
    fresh = suggest(seeded_session, owner, today=TODAY)[0]
    assert fresh.missing == []


def test_home_and_pantry_name_what_is_probably_gone(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    ingredient = _ing(seeded_session, "chicken breast")
    three_weeks_ago = datetime.now(UTC) - timedelta(days=21)
    record_event(
        seeded_session, web_user.id, ingredient, reason=EventReason.PURCHASE, delta=500.0,
        occurred_at=three_weeks_ago,
    )
    _item(seeded_session, web_user.id, ingredient).last_confirmed_at = three_weeks_ago
    seeded_session.commit()

    assert "Probably gone:</strong> chicken breast" in signed_in.get("/").text
    assert "<strong>probably gone</strong>" in signed_in.get("/pantry").text


def test_home_levels_use_the_pantry_card_and_return_home(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    ingredient = _ing(seeded_session, "chicken breast")
    long_ago = datetime.now(UTC) - timedelta(days=400)
    record_event(
        seeded_session, web_user.id, ingredient, reason=EventReason.MANUAL, delta=500.0,
        occurred_at=long_ago,
    )
    _item(seeded_session, web_user.id, ingredient).last_confirmed_at = long_ago
    seeded_session.commit()

    home = signed_in.get("/").text
    assert f'id="edit{ingredient.id}"' in home
    assert 'name="next" value="/"' in home

    confirmed = signed_in.post(
        f"/pantry/{ingredient.id}/confirm", data={"next": "/"}
    )
    assert confirmed.headers["location"] == "/"
    elsewhere = signed_in.post(
        f"/pantry/{ingredient.id}/confirm", data={"next": "https://example.com/x"}
    )
    assert elsewhere.headers["location"] == "/"
