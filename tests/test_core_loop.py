"""Pantry drift: shopped items, skipped meals, cook shortfalls, unnamed lines.

Each change is checked through the ledger or the page that records it. A level
is never asserted as a direct write.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.core.inventory import (
    INFERRED_PURCHASE_CONFIDENCE,
    consume_for_recipe,
    cover_cook_shortfall,
    get_or_create_item,
    pantry_view,
    record_event,
)
from souschef.core.recipes import save_imported_recipe
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    InventoryEvent,
    MealPlanEntry,
    MealSlot,
    PantryItem,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
    Recipe,
    RecipeIngredient,
    RecipeSource,
    ShoppingListItem,
)
from souschef.models.planning import ItemSource
from souschef.planning.shopping import (
    ShoppingError,
    add_manual_item,
    build_shopping_list,
    ingredients_stocked_since,
    stock_ticked_items,
    to_purchase_quantity,
    unnamed_recipe_lines,
)
from souschef.planning.suggest import Surface, is_ignorable, score_recipe


def ing(session: Session, name: str) -> CanonicalIngredient:
    found = session.scalar(select(CanonicalIngredient).where(CanonicalIngredient.name == name))
    assert found is not None, name
    return found


def _day(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def _pantry(session: Session, user_id: int) -> dict:
    return {view.ingredient.id: view for view in pantry_view(session, user_id)}


def _events(session: Session, user_id: int, ingredient_id: int) -> list[InventoryEvent]:
    return list(
        session.scalars(
            select(InventoryEvent)
            .where(
                InventoryEvent.user_id == user_id,
                InventoryEvent.ingredient_id == ingredient_id,
            )
            .order_by(InventoryEvent.id)
        )
    )


def _recipe(
    session: Session,
    user_id: int,
    title: str,
    lines: list[tuple[str, CanonicalIngredient | None, bool]],
) -> Recipe:
    recipe = Recipe(user_id=user_id, title=title, source=RecipeSource.MANUAL, servings=1)
    session.add(recipe)
    session.flush()
    for position, (raw, ingredient, optional) in enumerate(lines):
        session.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                position=position,
                raw_text=raw,
                ingredient_id=ingredient.id if ingredient is not None else None,
                quantity=200 if ingredient is not None else None,
                unit="g" if ingredient is not None else None,
                is_optional=optional,
            )
        )
    session.flush()
    return recipe


def _plan(
    session: Session,
    user_id: int,
    recipe: Recipe | None,
    day: date,
    *,
    title: str | None = None,
) -> MealPlanEntry:
    entry = MealPlanEntry(
        user_id=user_id,
        plan_date=day,
        slot=MealSlot.DINNER,
        recipe_id=recipe.id if recipe is not None else None,
        freeform_title=title,
        servings=1,
    )
    session.add(entry)
    session.flush()
    return entry


def _week(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    return today, today + timedelta(days=6)


def _item_id(html: str, label: str) -> int:
    marker = re.search(rf">\s*{re.escape(label)}\s*<", html)
    assert marker is not None, label
    window = html[max(0, marker.start() - 1200) : marker.start()]
    found = re.findall(r'action="/shopping/item/(\d+)/toggle"', window)
    assert found, label
    return int(found[-1])


def _imported_pasta(session: Session, user_id: int, title: str, url: str) -> Recipe:
    return save_imported_recipe(
        session,
        user_id,
        RawRecipe(
            title=title,
            ingredients=["1 pound spaghetti"],
            servings=1,
            source_url=url,
        ),
        source=RecipeSource.URL,
    )


def test_ticked_package_is_one_low_confidence_purchase_and_not_a_second(
    seeded_session: Session, owner: int
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    today, end = _week()
    recipe = _recipe(
        seeded_session, owner, "Thighs", [("200 g chicken thigh", chicken, False)]
    )
    _plan(seeded_session, owner, recipe, today)
    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    line = next(item for item in listing.items if item.ingredient_id == chicken.id)
    line.is_checked = True
    seeded_session.flush()

    added = stock_ticked_items(seeded_session, owner, listing)

    assert added == ["chicken thigh"]
    assert line.is_stocked
    assert get_or_create_item(seeded_session, owner, chicken).quantity == pytest.approx(907)
    purchase = _events(seeded_session, owner, chicken.id)[0]
    assert purchase.reason == EventReason.PURCHASE
    assert purchase.event_confidence == INFERRED_PURCHASE_CONFIDENCE
    assert purchase.event_confidence < 0.5
    assert purchase.source_ref == f"shopping:{listing.id}"
    assert purchase.note == "added from shopping list: chicken thigh"

    assert stock_ticked_items(seeded_session, owner, listing) == []
    assert get_or_create_item(seeded_session, owner, chicken).quantity == pytest.approx(907)
    assert len(_events(seeded_session, owner, chicken.id)) == 1


def test_rebuild_keeps_when_the_list_was_built_and_drops_a_covered_item(
    seeded_session: Session, owner: int
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    today, end = _week()
    recipe = _recipe(
        seeded_session, owner, "Thighs", [("200 g chicken thigh", chicken, False)]
    )
    _plan(seeded_session, owner, recipe, today)
    first = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    built_at = first.created_at
    line = next(item for item in first.items if item.ingredient_id == chicken.id)
    line.is_checked = True
    seeded_session.flush()
    stock_ticked_items(seeded_session, owner, first)

    second = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)

    assert _day(second.created_at) == _day(built_at)
    assert second.created_at == built_at
    assert chicken.id not in {item.ingredient_id for item in second.items if not item.is_skipped}
    assert ingredients_stocked_since(seeded_session, owner, second.created_at) == {chicken.id}


def test_a_purchase_from_before_the_list_was_built_is_not_treated_as_this_shop(
    seeded_session: Session, owner: int
) -> None:
    chicken = ing(seeded_session, "chicken thigh")
    record_event(
        seeded_session,
        owner,
        chicken,
        reason=EventReason.PURCHASE,
        delta=907,
        unit="g",
        source_ref="shopping:1",
        occurred_at=datetime.now(UTC) - timedelta(days=1),
    )
    today, end = _week()
    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)

    assert ingredients_stocked_since(seeded_session, owner, listing.created_at) == set()


def test_manual_item_with_no_catalog_match_lands_in_other_and_survives_rebuild(
    seeded_session: Session, owner: int
) -> None:
    today, end = _week()
    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    added = add_manual_item(seeded_session, listing, "paper towels", amount=2, unit="count")

    assert added.label == "paper towels"
    assert added.aisle == "Other"
    assert added.ingredient_id is None
    assert added.source == ItemSource.MANUAL
    assert added.purchase_qty == 2

    rebuilt = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    kept = next(item for item in rebuilt.items if item.label == "paper towels")
    assert kept.aisle == "Other"
    assert kept.source == ItemSource.MANUAL
    assert kept.purchase_qty == 2
    assert not kept.is_skipped


def test_manual_alias_takes_the_catalog_aisle(seeded_session: Session, owner: int) -> None:
    today, end = _week()
    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    added = add_manual_item(seeded_session, listing, "onion")

    assert added.label == "yellow onion"
    assert added.aisle == "Produce"
    assert added.ingredient_id == ing(seeded_session, "yellow onion").id


def test_a_staple_cannot_be_added_or_stocked_from_the_list(
    seeded_session: Session, owner: int
) -> None:
    oil = ing(seeded_session, "olive oil")
    today, end = _week()
    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    with pytest.raises(ShoppingError, match="olive oil is assumed on hand"):
        add_manual_item(seeded_session, listing, "olive oil")

    listing.items.append(
        ShoppingListItem(
            ingredient_id=oil.id,
            label=oil.name,
            needed_qty=1,
            purchase_qty=1,
            purchase_unit="tbsp",
            aisle="Other",
            is_checked=True,
        )
    )
    seeded_session.flush()

    assert stock_ticked_items(seeded_session, owner, listing) == []
    assert _events(seeded_session, owner, oil.id) == []


def test_skip_survives_rebuild_and_re_adding_clears_it(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    today, end = _week()
    recipe = _recipe(seeded_session, owner, "Noodles", [("200 g spaghetti", pasta, False)])
    _plan(seeded_session, owner, recipe, today)
    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    line = next(item for item in listing.items if item.ingredient_id == pasta.id)
    line.is_skipped = True
    seeded_session.flush()

    hidden = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    visible = [item.label for item in hidden.items if not item.is_skipped]
    assert "spaghetti" not in visible
    assert any(item.is_skipped and item.ingredient_id == pasta.id for item in hidden.items)

    add_manual_item(seeded_session, hidden, "spaghetti")
    restored = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)
    shown = [item for item in restored.items if item.label == "spaghetti" and not item.is_skipped]
    assert len(shown) == 1
    assert not any(item.is_skipped and item.ingredient_id == pasta.id for item in restored.items)


def test_skipped_and_old_meals_do_not_drive_the_list(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    today, end = _week()
    recipe = _recipe(seeded_session, owner, "Noodles", [("200 g spaghetti", pasta, False)])
    skipped = _plan(seeded_session, owner, recipe, today)
    skipped.skipped_at = datetime.now(UTC)
    _plan(seeded_session, owner, recipe, today - timedelta(days=8))
    seeded_session.flush()

    listing = build_shopping_list(seeded_session, owner, covers_from=today, covers_to=end)

    assert [item for item in listing.items if not item.is_skipped] == []
    assert unnamed_recipe_lines(seeded_session, owner, today, end) == []


def test_unnamed_section_lists_non_optional_lines_once(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    today, end = _week()
    recipe = _recipe(
        seeded_session,
        owner,
        "Mystery stew",
        [
            ("200 g spaghetti", pasta, False),
            ("1 cup xyzzybroth", None, False),
            ("parsley, for garnish", None, True),
        ],
    )
    _plan(seeded_session, owner, recipe, today)
    _plan(seeded_session, owner, recipe, today + timedelta(days=1))

    lines = unnamed_recipe_lines(seeded_session, owner, today, end)

    assert lines == [(recipe.id, "Mystery stew", "1 cup xyzzybroth")]


def test_readiness_names_unidentified_lines_and_ignores_optional_ones(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    chicken = ing(seeded_session, "chicken thigh")
    record_event(seeded_session, owner, pasta, reason=EventReason.PURCHASE, delta=500)
    pantry = _pantry(seeded_session, owner)

    stocked = _recipe(
        seeded_session,
        owner,
        "Named",
        [("200 g spaghetti", pasta, False), ("1 cup xyzzybroth", None, False)],
    )
    suggestion = score_recipe(seeded_session, stocked, pantry)
    assert suggestion.reason == "You have everything named · 1 line not identified"

    two = _recipe(
        seeded_session,
        owner,
        "Two gaps",
        [
            ("200 g spaghetti", pasta, False),
            ("1 cup xyzzybroth", None, False),
            ("a knob of nam pla special", None, False),
        ],
    )
    assert score_recipe(seeded_session, two, pantry).reason == (
        "You have everything named · 2 lines not identified"
    )

    short = _recipe(
        seeded_session,
        owner,
        "Short one",
        [
            ("200 g chicken thigh", chicken, False),
            ("200 g spaghetti", pasta, False),
            ("1 cup xyzzybroth", None, False),
        ],
    )
    missing = score_recipe(seeded_session, short, pantry)
    assert missing.reason == "Just needs chicken thigh · 1 line not identified"

    garnish = _recipe(
        seeded_session,
        owner,
        "Garnish only",
        [("200 g spaghetti", pasta, False), ("parsley, for garnish", None, True)],
    )
    assert score_recipe(seeded_session, garnish, _pantry(seeded_session, owner)).reason == (
        "You have everything"
    )


def test_cooking_more_than_the_ledger_holds_records_a_purchase_first(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, owner, pasta, reason=EventReason.PURCHASE, delta=1)
    when = datetime(2026, 9, 29, 12, tzinfo=UTC)

    purchases = cover_cook_shortfall(
        seeded_session, owner, {pasta.id: 6}, recipe_id=9, occurred_at=when
    )
    cooks = consume_for_recipe(
        seeded_session, owner, {pasta.id: 6}, recipe_id=9, occurred_at=when
    )

    assert get_or_create_item(seeded_session, owner, pasta).quantity == pytest.approx(0)
    assert [event.reason for event in purchases] == [EventReason.PURCHASE]
    assert purchases[0].event_confidence == INFERRED_PURCHASE_CONFIDENCE
    assert purchases[0].source_ref == "recipe:9"
    assert purchases[0].note == "implied purchase: cooked more than was on hand"
    assert purchases[0].delta == pytest.approx(5)
    assert cooks[0].reason == EventReason.COOK
    assert cooks[0].delta == pytest.approx(-6)
    assert purchases[0].id < cooks[0].id
    assert purchases[0].occurred_at == when
    assert cooks[0].occurred_at == when


def test_a_cook_that_fits_the_pantry_does_not_invent_a_purchase(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, owner, pasta, reason=EventReason.PURCHASE, delta=10)

    assert cover_cook_shortfall(seeded_session, owner, {pasta.id: 4}, recipe_id=3) == []
    cooks = consume_for_recipe(seeded_session, owner, {pasta.id: 4}, recipe_id=3)

    assert [event.reason for event in cooks] == [EventReason.COOK]
    assert get_or_create_item(seeded_session, owner, pasta).quantity == pytest.approx(6)


def test_a_staple_shortfall_is_not_purchased_or_debited(
    seeded_session: Session, owner: int
) -> None:
    oil = ing(seeded_session, "olive oil")
    record_event(seeded_session, owner, oil, reason=EventReason.PURCHASE, delta=10)

    assert cover_cook_shortfall(seeded_session, owner, {oil.id: 50}, recipe_id=4) == []
    assert consume_for_recipe(seeded_session, owner, {oil.id: 50}, recipe_id=4) == []
    assert get_or_create_item(seeded_session, owner, oil).quantity == pytest.approx(10)


def test_shopping_page_adds_ticked_items_at_package_size(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Pasta Night", "https://example.com/core-loop-pasta"
    )
    _plan(seeded_session, web_user.id, recipe, date.today() + timedelta(days=1))
    seeded_session.commit()

    page = signed_in.get("/shopping")
    assert page.status_code == 200
    assert "Add 1 ticked" not in page.text
    item_id = _item_id(page.text, "spaghetti")

    assert signed_in.post(f"/shopping/item/{item_id}/toggle").status_code == 303
    ticked = signed_in.get("/shopping")
    assert "Add 1 ticked item to pantry" in ticked.text

    stocked = signed_in.post("/shopping/stock")
    assert stocked.status_code == 303
    assert stocked.headers["location"].startswith("/shopping?added=spaghetti")
    followed = signed_in.get(stocked.headers["location"])
    assert "Added spaghetti to the pantry." in followed.text
    assert "Add 1 ticked item to pantry" not in followed.text

    seeded_session.expire_all()
    pasta = ing(seeded_session, "spaghetti")
    assert get_or_create_item(seeded_session, web_user.id, pasta).quantity == pytest.approx(454)
    purchase = _events(seeded_session, web_user.id, pasta.id)[0]
    assert purchase.event_confidence == INFERRED_PURCHASE_CONFIDENCE
    assert purchase.source_ref.startswith("shopping:")

    again = signed_in.post("/shopping/stock")
    assert again.status_code == 303
    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, pasta).quantity == pytest.approx(454)


def test_two_ticked_lines_use_the_plural_button(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Plural Pasta", "https://example.com/core-loop-plural"
    )
    _plan(seeded_session, web_user.id, recipe, date.today())
    seeded_session.commit()
    signed_in.post("/shopping/add", data={"name": "paper towels"})
    page = signed_in.get("/shopping")
    pasta_id = _item_id(page.text, "spaghetti")
    towels_id = _item_id(page.text, "paper towels")
    signed_in.post(f"/shopping/item/{pasta_id}/toggle")
    signed_in.post(f"/shopping/item/{towels_id}/toggle")

    ticked = signed_in.get("/shopping")

    assert "Add 2 ticked items to pantry" in ticked.text


def test_a_bad_unit_on_a_ticked_line_adds_nothing(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Bad Unit Pasta", "https://example.com/core-loop-bad-unit"
    )
    _plan(seeded_session, web_user.id, recipe, date.today())
    seeded_session.commit()
    signed_in.post("/shopping/add", data={"name": "paper towels", "amount": "2", "unit": "bogus"})
    page = signed_in.get("/shopping")
    signed_in.post(f"/shopping/item/{_item_id(page.text, 'spaghetti')}/toggle")
    signed_in.post(f"/shopping/item/{_item_id(page.text, 'paper towels')}/toggle")

    failed = signed_in.post("/shopping/stock")

    assert failed.status_code == 400
    assert "Unknown unit" in failed.text
    seeded_session.expire_all()
    pasta = ing(seeded_session, "spaghetti")
    row = seeded_session.scalar(
        select(PantryItem).where(
            PantryItem.user_id == web_user.id, PantryItem.ingredient_id == pasta.id
        )
    )
    assert row is None


def test_manual_lines_and_skips_survive_the_page_rebuild(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Skip Pasta", "https://example.com/core-loop-skip"
    )
    _plan(seeded_session, web_user.id, recipe, date.today())
    seeded_session.commit()

    added = signed_in.post("/shopping/add", data={"name": "paper towels"})
    assert added.status_code == 303
    page = signed_in.get("/shopping")
    assert "paper towels" in page.text
    assert ">Other<" in page.text
    assert signed_in.get("/shopping").text.count("paper towels") >= 1

    onion = signed_in.post("/shopping/add", data={"name": "onion"})
    assert onion.status_code == 303
    named = signed_in.get("/shopping")
    assert "yellow onion" in named.text
    assert ">Produce<" in named.text

    staple = signed_in.post("/shopping/add", data={"name": "olive oil"})
    assert staple.status_code == 400
    assert "olive oil is assumed on hand, so it stays off the list." in staple.text

    garbled = signed_in.post("/shopping/add", data={"name": "rice paper", "amount": "nope"})
    assert garbled.status_code == 400
    assert "Amount isn&#39;t a number." in garbled.text

    current = signed_in.get("/shopping")
    signed_in.post(f"/shopping/item/{_item_id(current.text, 'spaghetti')}/skip")
    after_skip = signed_in.get("/shopping")
    assert "spaghetti" not in after_skip.text
    assert "paper towels" in after_skip.text
    still = signed_in.get("/shopping")
    assert "spaghetti" not in still.text

    signed_in.post("/shopping/add", data={"name": "spaghetti"})
    restored = signed_in.get("/shopping")
    assert "spaghetti" in restored.text


def test_shopping_page_names_unidentified_recipe_lines(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    recipe = _recipe(
        seeded_session,
        web_user.id,
        "Mystery stew",
        [
            ("200 g spaghetti", pasta, False),
            ("1 cup xyzzybroth", None, False),
            ("parsley, for garnish", None, True),
        ],
    )
    _plan(seeded_session, web_user.id, recipe, date.today())
    seeded_session.commit()

    page = signed_in.get("/shopping")

    assert "Not identified — name these on the recipe" in page.text
    assert f'href="/recipes/{recipe.id}"' in page.text
    assert "1 cup xyzzybroth" in page.text
    assert "for garnish" not in page.text


def test_receipt_review_unticks_what_the_list_already_added(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    garlic = ing(seeded_session, "garlic")
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Receipt Pasta", "https://example.com/core-loop-receipt"
    )
    _plan(seeded_session, web_user.id, recipe, date.today())
    receipt = Receipt(
        user_id=web_user.id,
        image_path="test.jpg",
        status=ReceiptStatus.NEEDS_REVIEW,
        ocr_confidence=0.95,
    )
    seeded_session.add(receipt)
    seeded_session.flush()
    pasta_line = ReceiptLine(
        receipt_id=receipt.id,
        position=0,
        raw_text="SPAGHETTI",
        parsed_quantity=454,
        parsed_unit="g",
        ingredient_id=pasta.id,
        match_confidence=0.95,
        is_confirmed=True,
    )
    garlic_line = ReceiptLine(
        receipt_id=receipt.id,
        position=1,
        raw_text="GARLIC",
        parsed_quantity=1,
        parsed_unit="count",
        ingredient_id=garlic.id,
        match_confidence=0.95,
        is_confirmed=True,
    )
    seeded_session.add_all([pasta_line, garlic_line])
    seeded_session.commit()

    shop = signed_in.get("/shopping")
    signed_in.post(f"/shopping/item/{_item_id(shop.text, 'spaghetti')}/toggle")
    assert signed_in.post("/shopping/stock").status_code == 303
    signed_in.get("/shopping")

    review = signed_in.get(f"/receipts/{receipt.id}")

    assert review.status_code == 200
    assert f'name="confirmed_{pasta_line.id}" checked' not in review.text
    assert "Already added from the shopping list, so this line stays unticked." in review.text
    assert f'name="confirmed_{garlic_line.id}" checked' in review.text

    seeded_session.expire_all()
    before = get_or_create_item(seeded_session, web_user.id, pasta).quantity
    applied = signed_in.post(
        f"/receipts/{receipt.id}/apply",
        data={
            f"ingredient_{pasta_line.id}": str(pasta.id),
            f"quantity_{pasta_line.id}": "454",
            f"unit_{pasta_line.id}": "g",
            f"ingredient_{garlic_line.id}": str(garlic.id),
            f"quantity_{garlic_line.id}": "1",
            f"unit_{garlic_line.id}": "count",
            f"confirmed_{garlic_line.id}": "on",
        },
    )
    assert applied.status_code == 200
    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, pasta).quantity == pytest.approx(before)
    assert get_or_create_item(seeded_session, web_user.id, garlic).quantity == pytest.approx(1)


def test_a_line_bought_before_this_list_stays_ticked(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(
        seeded_session,
        web_user.id,
        pasta,
        reason=EventReason.PURCHASE,
        delta=454,
        unit="g",
        source_ref="shopping:1",
        occurred_at=datetime.now(UTC) - timedelta(days=1),
    )
    receipt = Receipt(
        user_id=web_user.id,
        image_path="earlier.jpg",
        status=ReceiptStatus.NEEDS_REVIEW,
        ocr_confidence=0.95,
    )
    seeded_session.add(receipt)
    seeded_session.flush()
    line = ReceiptLine(
        receipt_id=receipt.id,
        position=0,
        raw_text="SPAGHETTI",
        parsed_quantity=454,
        parsed_unit="g",
        ingredient_id=pasta.id,
        match_confidence=0.95,
        is_confirmed=True,
    )
    seeded_session.add(line)
    seeded_session.commit()
    signed_in.get("/shopping")

    review = signed_in.get(f"/receipts/{receipt.id}")

    assert f'name="confirmed_{line.id}" checked' in review.text
    assert "Already added from the shopping list" not in review.text


def test_home_asks_about_uncooked_meals_from_the_last_week(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    yesterday = _imported_pasta(
        seeded_session, web_user.id, "Yesterday Pasta", "https://example.com/core-loop-yest"
    )
    today_recipe = _imported_pasta(
        seeded_session, web_user.id, "Today Pasta", "https://example.com/core-loop-today"
    )
    ancient = _imported_pasta(
        seeded_session, web_user.id, "Ancient Pasta", "https://example.com/core-loop-old"
    )
    missed = _plan(seeded_session, web_user.id, yesterday, date.today() - timedelta(days=1))
    _plan(seeded_session, web_user.id, today_recipe, date.today())
    too_old = _plan(seeded_session, web_user.id, ancient, date.today() - timedelta(days=8))
    _plan(
        seeded_session,
        web_user.id,
        None,
        date.today() - timedelta(days=1),
        title="Ate leftovers",
    )
    seeded_session.commit()

    page = signed_in.get("/")

    assert "Did you cook these?" in page.text
    assert "Yesterday" in page.text
    assert f'/recipes/{yesterday.id}/cook?entry={missed.id}' in page.text
    assert f'/plan/{missed.id}/skip' in page.text
    assert f'/recipes/{ancient.id}/cook?entry={too_old.id}' not in page.text
    assert f'/plan/{too_old.id}/skip' not in page.text
    assert "Ate leftovers" not in page.text


def test_plan_keeps_the_last_week_and_hides_older_meals(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recent = _imported_pasta(
        seeded_session, web_user.id, "Recent Pasta", "https://example.com/core-loop-recent"
    )
    ancient = _imported_pasta(
        seeded_session, web_user.id, "Ancient Pasta", "https://example.com/core-loop-ancient"
    )
    entry = _plan(seeded_session, web_user.id, recent, date.today() - timedelta(days=1))
    too_old = _plan(seeded_session, web_user.id, ancient, date.today() - timedelta(days=8))
    seeded_session.commit()

    page = signed_in.get("/plan")

    assert f'/recipes/{recent.id}/cook?entry={entry.id}' in page.text
    assert f'/recipes/{ancient.id}/cook?entry={too_old.id}' not in page.text


def test_skipping_a_past_meal_keeps_it_and_debits_nothing(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Skipped Pasta", "https://example.com/core-loop-skipped"
    )
    entry = _plan(seeded_session, web_user.id, recipe, date.today() - timedelta(days=1))
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=500)
    seeded_session.commit()
    before = seeded_session.scalar(
        select(func.count())
        .select_from(InventoryEvent)
        .where(InventoryEvent.user_id == web_user.id)
    )

    skipped = signed_in.post(f"/plan/{entry.id}/skip", data={"next": "/"})

    assert skipped.status_code == 303
    assert skipped.headers["location"] == "/"
    seeded_session.expire_all()
    saved = seeded_session.get(MealPlanEntry, entry.id)
    assert saved is not None
    assert saved.skipped_at is not None
    assert saved.cooked_at is None
    after = seeded_session.scalar(
        select(func.count())
        .select_from(InventoryEvent)
        .where(InventoryEvent.user_id == web_user.id)
    )
    assert after == before
    assert get_or_create_item(seeded_session, web_user.id, pasta).quantity == pytest.approx(500)

    home = signed_in.get("/")
    assert "Did you cook these?" not in home.text
    plan = signed_in.get("/plan")
    assert "Skipped Pasta" in plan.text
    assert ">Skipped<" in plan.text
    assert f'/recipes/{recipe.id}/cook?entry={entry.id}' not in plan.text


def test_cook_preview_warns_names_untracked_lines_and_offers_the_past_week(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=10)
    recipe = _recipe(
        seeded_session,
        web_user.id,
        "Late Pasta",
        [("200 g spaghetti", pasta, False), ("1 cup xyzzybroth", None, False)],
    )
    entry = _plan(seeded_session, web_user.id, recipe, date.today() - timedelta(days=1))
    seeded_session.commit()

    page = signed_in.get(f"/recipes/{recipe.id}/cook?entry={entry.id}")

    assert page.status_code == 200
    assert "Using more than you have" in page.text
    assert "Not tracked" in page.text
    assert "1 cup xyzzybroth" in page.text
    assert "These lines aren’t in the pantry, so cooking doesn’t change them." in page.text
    assert 'id="cooked_on"' in page.text
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert f'value="{yesterday}" selected' in page.text

    bare = signed_in.get(f"/recipes/{recipe.id}/cook")
    assert 'name="cooked_on"' in bare.text
    assert f'value="{date.today().isoformat()}" selected' in bare.text


def test_cooking_on_a_past_day_closes_that_entry_and_covers_the_shortfall(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=10)
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Backdated Pasta", "https://example.com/core-loop-backdate"
    )
    yesterday = date.today() - timedelta(days=1)
    entry = _plan(seeded_session, web_user.id, recipe, yesterday)
    today_entry = _plan(seeded_session, web_user.id, recipe, date.today())
    seeded_session.commit()

    cooked = signed_in.post(
        f"/recipes/{recipe.id}/cooked",
        data={"servings": "1", "cooked_on": yesterday.isoformat()},
    )

    assert cooked.status_code == 303
    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, pasta).quantity == pytest.approx(0)
    events = _events(seeded_session, web_user.id, pasta.id)
    purchase = next(
        event
        for event in events
        if event.reason == EventReason.PURCHASE
        and event.note
        and "implied purchase" in event.note
    )
    cook = next(event for event in events if event.reason == EventReason.COOK)
    assert purchase.id < cook.id
    assert purchase.event_confidence == INFERRED_PURCHASE_CONFIDENCE
    assert _day(purchase.occurred_at) == yesterday
    assert _day(cook.occurred_at) == yesterday
    closed = seeded_session.get(MealPlanEntry, entry.id)
    left_open = seeded_session.get(MealPlanEntry, today_entry.id)
    assert closed is not None and closed.cooked_at is not None
    assert _day(closed.cooked_at) == yesterday
    assert left_open is not None and left_open.cooked_at is None
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 1


def test_a_cook_date_outside_the_week_changes_nothing(
    signed_in: TestClient, seeded_session: Session, web_user
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, web_user.id, pasta, reason=EventReason.PURCHASE, delta=10)
    recipe = _imported_pasta(
        seeded_session, web_user.id, "Bad Date Pasta", "https://example.com/core-loop-baddate"
    )
    entry = _plan(seeded_session, web_user.id, recipe, date.today() - timedelta(days=1))
    seeded_session.commit()

    rejected = signed_in.post(
        f"/recipes/{recipe.id}/cooked",
        data={"servings": "1", "cooked_on": "1999-01-01", "entry_id": str(entry.id)},
    )

    assert rejected.status_code == 400
    assert "Pick a day from today back to a week ago." in rejected.text
    seeded_session.expire_all()
    assert get_or_create_item(seeded_session, web_user.id, pasta).quantity == pytest.approx(10)
    assert seeded_session.get(MealPlanEntry, entry.id).cooked_at is None
    assert seeded_session.get(Recipe, recipe.id).times_cooked == 0


def test_a_shortfall_within_one_percent_of_a_package_buys_that_package(
    seeded_session: Session,
) -> None:
    milk = ing(seeded_session, "whole milk")
    milk.package_qty, milk.package_unit = 946, "ml"

    assert to_purchase_quantity(milk, 946.35)[0] == 1
    assert to_purchase_quantity(milk, 1000)[0] == 2
    assert to_purchase_quantity(milk, 5)[0] == 1


def test_unnamed_required_lines_count_toward_readiness(
    seeded_session: Session, owner: int
) -> None:
    pasta = ing(seeded_session, "spaghetti")
    record_event(seeded_session, owner, pasta, reason=EventReason.PURCHASE, delta=500)
    pantry = _pantry(seeded_session, owner)
    stocked = ("200 g spaghetti", pasta, False)

    def surface(title: str, *unmatched: str):  # noqa: ANN202
        lines = [stocked, *((text, None, False) for text in unmatched)]
        recipe = _recipe(seeded_session, owner, title, lines)
        return score_recipe(seeded_session, recipe, pantry).surface

    assert surface("Clean") == Surface.COOK_NOW
    assert surface("One gap", "1 cup xyzzybroth") == Surface.NEARLY_THERE
    assert surface("Five gaps", *(f"{n} cup xyzzy{n}" for n in range(5))) == Surface.REACH
    assert surface("Water only", "2 tbsp water") == Surface.COOK_NOW
    assert surface("Serve only", "xyzzy wedges, to serve") == Surface.COOK_NOW


def test_is_ignorable_matches_plain_water_and_serving_lines_only() -> None:
    assert is_ignorable("1 cup warm water")
    assert is_ignorable("Rice, for serving")
    assert not is_ignorable("2 cups watercress")
    assert not is_ignorable("1 cup chicken stock")
