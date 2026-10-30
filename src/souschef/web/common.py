"""Helpers shared by more than one router."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import Session

from souschef import logconfig
from souschef.core.matching import IngredientMatcher, MatchMethod
from souschef.core.units import (
    NeedsPackage,
    UnitChip,
    UnitConversionError,
    add_unit_choices,
    base_unit_for,
    dimension_of,
    entry_form_unit,
    entry_label,
    format_entry_amount,
    format_quantity,
    normalize_unit,
    package_caption,
    parse_input_unit,
)
from souschef.models import CanonicalIngredient, EventReason, InventoryEvent, Recipe, User
from souschef.models.base import Base
from souschef.planning.next_step import recipe_readiness
from souschef.planning.shopping import build_shopping_list, is_package_count

PLAN_DAYS = 10


# Uncooked meals stay visible this long, and a cook can be logged back this far.
PAST_DAYS = 7


WEB_DIR = Path(__file__).parent


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _user_system(context: dict) -> tuple[str, bool]:
    user = context.get("user")
    system = getattr(user, "unit_system", None) or "us"
    if system not in ("us", "metric"):
        system = "us"
    return system, bool(getattr(user, "show_alt_units", False))


def _format_for(
    value: float,
    unit: str,
    ingredient: CanonicalIngredient | None,
    system: str,
    show_alt: bool,
    *,
    noun: str | None = None,
    name: str | None = None,
) -> str:
    return format_quantity(
        value,
        unit,
        system=system,
        show_alt=show_alt,
        noun=ingredient.count_noun if ingredient is not None and noun is None else noun,
        name=(ingredient.name if ingredient is not None else "") if name is None else name,
    )


@pass_context
def _quantity(
    context: dict,
    value: float,
    ingredient: CanonicalIngredient,
    unit: str | None = None,
    round_count: bool = False,
) -> str:
    """A quantity in the signed-in user's unit system.

    ``unit`` defaults to the ingredient's base unit. A named count word ("clove",
    "head") is kept as written instead of being restated in the base count.
    """
    system, show_alt = _user_system(context)
    chosen = unit or ingredient.base_unit
    shown = float(value)
    normalized = normalize_unit(chosen)
    if round_count and dimension_of(normalized) == "count":
        shown = float(round(shown))
    if unit and dimension_of(normalized) == "count" and normalized != "count":
        return format_quantity(shown, normalized, noun=normalized, name="")
    return _format_for(shown, chosen, ingredient, system, show_alt)


def _stock_amount(
    quantity: float, ingredient: CanonicalIngredient, system: str, show_alt: bool
) -> str:
    """Pantry levels. A remainder below one base unit reads as out, not "0.4 g"."""
    if 0 < quantity < 1:
        return "out"
    return _format_for(float(quantity), ingredient.base_unit, ingredient, system, show_alt)


@pass_context
def _stock(context: dict, quantity: float, ingredient: CanonicalIngredient) -> str:
    system, show_alt = _user_system(context)
    return _stock_amount(float(quantity), ingredient, system, show_alt)


def _entry_value(value: float | None) -> float | None:
    if value is None:
        return None
    return float(value)


@pass_context
def _unit_label(context: dict, ingredient: CanonicalIngredient, value: float | None = None) -> str:
    system, _show_alt = _user_system(context)
    return entry_label(ingredient, system, _entry_value(value))


@pass_context
def _form_unit(context: dict, ingredient: CanonicalIngredient, value: float | None = None) -> str:
    system, _show_alt = _user_system(context)
    return entry_form_unit(ingredient, system, _entry_value(value))


@pass_context
def _entry_amount(context: dict, value: float, ingredient: CanonicalIngredient) -> str:
    system, _show_alt = _user_system(context)
    return format_entry_amount(float(value), ingredient, system)


def _plain_amount(qty: float) -> str:
    if abs(qty - round(qty)) < 0.05:
        return str(int(round(qty)))
    return f"{qty:g}"


def _count_prefix(
    qty: float, ingredient: CanonicalIngredient | None, system: str, show_alt: bool
) -> str:
    if ingredient is None:
        return f"{_plain_amount(qty)}×"
    formatted = _format_for(qty, "count", ingredient, system, show_alt)
    if " dozen" in formatted:
        return formatted
    # The amount keeps the count noun even when that noun is the food ("2 onions").
    if ingredient.count_noun:
        return formatted
    return f"{_plain_amount(qty)}×"


def _package_noun(ingredient: object | None) -> str:
    return (getattr(ingredient, "package_noun", None) or "").strip()


def _one_count_package(ingredient: object | None) -> bool:
    """A package that is one counted item, not a bag of several."""
    if ingredient is None:
        return False
    qty = getattr(ingredient, "package_qty", None)
    unit = (getattr(ingredient, "package_unit", None) or "").strip()
    noun = (getattr(ingredient, "count_noun", None) or "").strip()
    return bool(qty) and float(qty) == 1 and unit == "count" and bool(noun)


def purchase_form_unit(item: object) -> str:
    """The unit box word: the noun the row already shows, else the stored unit."""
    unit = (getattr(item, "purchase_unit", None) or "").strip()
    ingredient = getattr(item, "ingredient", None)
    if ingredient is None:
        return unit
    if is_package_count(unit):
        noun = _package_noun(ingredient)
        if noun:
            return noun
        if _one_count_package(ingredient):
            return (ingredient.count_noun or "").strip()
        return unit
    if unit in {"", "count"}:
        noun = (getattr(ingredient, "count_noun", None) or "").strip()
        if noun:
            return noun
    return unit


def purchase_qty_text(item: object, *, system: str, show_alt: bool) -> str:
    """The amount in front of a shopping-list name, in the viewer's unit system."""
    qty = getattr(item, "purchase_qty", None)
    if not qty:
        return ""
    unit = (getattr(item, "purchase_unit", None) or "").strip()
    ingredient = getattr(item, "ingredient", None)
    amount = float(qty)
    if is_package_count(unit):
        packages = int(round(amount))
        # A container word is the thing you pick up ("1 bottle"), not the recipe measure.
        noun = _package_noun(ingredient)
        if noun:
            return _format_for(
                float(packages), "count", ingredient, system, show_alt, noun=noun, name=""
            )
        # One count per package is the item itself: "1 onion", not "1 × 1 onion".
        if _one_count_package(ingredient):
            return _format_for(float(packages), "count", ingredient, system, show_alt)
        if ingredient is not None and ingredient.package_qty and ingredient.package_unit:
            size = _format_for(
                ingredient.package_qty,
                ingredient.package_unit,
                ingredient,
                system,
                show_alt,
            )
            return f"{packages} × {size}"
        return f"{packages}×"
    measure = unit or (ingredient.base_unit if ingredient is not None else "count")
    if measure == "count":
        return _count_prefix(amount, ingredient, system, show_alt)
    try:
        dimension_of(measure)
    except UnitConversionError:
        return f"{_plain_amount(amount)} {measure}".strip()
    return _format_for(amount, measure, ingredient, system, show_alt)


def purchase_need_text(item: object, *, system: str, show_alt: bool) -> str:
    """The recipe shortfall under a package line. Empty when the line is the shortfall."""
    if not is_package_count(getattr(item, "purchase_unit", None)):
        return ""
    needed = getattr(item, "needed_qty", None) or 0
    if needed <= 0:
        return ""
    ingredient = getattr(item, "ingredient", None)
    if ingredient is None:
        return ""
    amount = _format_for(needed, ingredient.base_unit, ingredient, system, show_alt)
    return f"needs {amount}"


@pass_context
def _shop_qty(context: dict, item: object) -> str:
    system, show_alt = _user_system(context)
    return purchase_qty_text(item, system=system, show_alt=show_alt)


@pass_context
def _shop_need(context: dict, item: object) -> str:
    system, show_alt = _user_system(context)
    return purchase_need_text(item, system=system, show_alt=show_alt)


def _receipt_label(status: object) -> str:
    key = getattr(status, "value", status)
    return {
        "uploaded": "Uploaded",
        "ocr_running": "Scanning",
        "needs_review": "Needs review",
        "applied": "Added to the pantry",
        "failed": "Couldn't scan",
    }.get(str(key), str(key).replace("_", " "))


_VULGAR = {
    "1/2": "½", "1/3": "⅓", "2/3": "⅔", "1/4": "¼", "3/4": "¾",
    "1/8": "⅛", "3/8": "⅜", "5/8": "⅝", "7/8": "⅞",
}
_TYPED_FRACTION = re.compile(r"\b(?:(\d+) )?([1-7]/[2348])\b")


def _fractions(text: str) -> str:
    # In the body font the space in "1 1/2" is too narrow to see, so it reads "11/2".
    def glyph(match: re.Match[str]) -> str:
        whole, fraction = match.groups()
        if fraction not in _VULGAR:
            return match.group(0)
        return f"{whole or ''}{_VULGAR[fraction]}"

    return _TYPED_FRACTION.sub(glyph, text or "")


STATIC_DIR = WEB_DIR.parent / "static"


def _static_url(name: str) -> str:
    # Static files carry no Cache-Control, so browsers keep a stylesheet for a
    # heuristic share of its age. The mtime in the URL makes an edit a new URL.
    return f"/static/{name}?v={int((STATIC_DIR / name).stat().st_mtime)}"


templates.env.globals["static_url"] = _static_url
templates.env.filters["quantity"] = _quantity
templates.env.filters["stock"] = _stock
templates.env.filters["unit_label"] = _unit_label
templates.env.filters["form_unit"] = _form_unit
templates.env.filters["entry_amount"] = _entry_amount
templates.env.filters["shop_qty"] = _shop_qty
templates.env.filters["shop_need"] = _shop_need
templates.env.filters["shop_unit"] = purchase_form_unit
templates.env.globals["recipe_readiness"] = recipe_readiness
templates.env.filters["receipt_label"] = _receipt_label
templates.env.filters["fractions"] = _fractions


logconfig.configure()


log = logconfig.get_logger("souschef.web")


THEME_COOKIE = "theme"


THEMES = ("light", "dark", "auto")


def _theme(request: Request) -> str:
    chosen = request.cookies.get(THEME_COOKIE, "")
    return chosen if chosen in THEMES else "light"


def _render(request: Request, template: str, *, status_code: int = 200, **context) -> HTMLResponse:
    # `user` is injected rather than passed by each route: the nav needs it on
    # every page, and a route that forgot it would render without the tab bar.
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("theme", _theme(request))
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _safe_next(value: str | None) -> str:
    """A same-origin path to return to after a form post.

    The share target's query string contains a full URL, so ``://`` is only
    rejected in the path. An absolute or scheme-relative value is dropped.
    """
    if not value:
        return "/"
    value = value.strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or value.startswith("/\\")
        or "\\" in value
        or "\r" in value
        or "\n" in value
        or "\x00" in value
    ):
        return "/"
    path = value.split("?", 1)[0].split("#", 1)[0]
    if "://" in path:
        return "/"
    return value


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _kitchen_unit_message(exc: UnitConversionError, unit: str) -> str:
    """The bag message, for a count-word we also can't weigh (a bunch, a head)."""
    text = str(exc)
    if "Don't know how much" in text:
        return text
    if "per-item weight" in text:
        word = unit.strip().lower().rstrip(".") or "of that"
        return f"Don't know how much one {word} weighs. Enter grams, ounces, pounds, or a count."
    return text


def _on_hand(view) -> bool:  # noqa: ANN001
    return not view.is_staple and view.quantity >= 1


def _horizon() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=PLAN_DAYS - 1)


def _sync_shopping(db: Session, user_id: int):  # noqa: ANN202
    start, end = _horizon()
    return build_shopping_list(db, user_id, covers_from=start, covers_to=end)


def _owned(db: Session, model: type[Base], obj_id: int, user: User, what: str):  # noqa: ANN201
    """Fetch a user-owned row by id, or 404.

    "Belongs to someone else" and "does not exist" deliberately return the same
    404. Distinguishing them would let one user probe which ids are real in
    another user's kitchen, and there is nothing useful to do with the
    difference anyway.
    """
    obj = db.get(model, obj_id)
    if obj is None or obj.user_id != user.id:  # type: ignore[attr-defined]
        raise HTTPException(status_code=404, detail=f"No such {what}")
    return obj


def request_hostname(request: Request) -> str:
    """The Host header without its port, lowercased; bracketed IPv6 loses its brackets."""
    host = request.headers.get("host", "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _viewer_units(user: User) -> tuple[str, bool]:
    system = user.unit_system or "us"
    if system not in ("us", "metric"):
        system = "us"
    return system, bool(user.show_alt_units)


def _change_toast(db: Session, user: User, event_id: int) -> dict | None:
    """The undo toast for one of this user's live stock edits.

    The words come from the ledger row, never from text in the URL. A voided
    event, or one that belongs to someone else, says nothing.
    """
    if not event_id:
        return None
    event = db.get(InventoryEvent, event_id)
    if event is None or event.user_id != user.id or event.voided_at is not None:
        return None
    ingredient = db.get(CanonicalIngredient, event.ingredient_id)
    if ingredient is None:
        return None
    name = ingredient.name
    system, show_alt = _viewer_units(user)
    if event.reason == EventReason.CONFIRM:
        message = f"Confirmed {name}"
    elif event.reason == EventReason.MANUAL:
        level = _stock_amount(event.resulting_quantity, ingredient, system, show_alt)
        message = f"Set {name} to {level}"
    elif event.reason == EventReason.PURCHASE:
        added = _stock_amount(event.delta, ingredient, system, show_alt)
        message = f"Added {added} {name}"
    else:
        return None
    return {"message": message, "event_id": event.id}


def ingredient_for_add(db: Session, name: str) -> CanonicalIngredient | None:
    """The catalog row an add form would reuse, or None for a new name."""
    clean = name.strip()
    if not clean:
        return None
    match = IngredientMatcher(db).match(clean)
    if match.method not in (MatchMethod.NAME_EXACT, MatchMethod.ALIAS_EXACT):
        return None
    if match.ingredient_id is None:
        return None
    return db.get(CanonicalIngredient, match.ingredient_id)


def add_form_units(
    db: Session, name: str, posted_unit: str, system: str
) -> tuple[list[UnitChip], str]:
    """Unit chips and the package caption for an add sheet that already names an ingredient."""
    ingredient = ingredient_for_add(db, name)
    if ingredient is None:
        return [], ""
    return add_unit_choices(ingredient, posted_unit), package_caption(ingredient, system or "us")


def _adhoc_units(unit: str) -> tuple[str, str | None]:
    """Base unit and the input unit for a typed pantry item that is not a package.

    An empty field is a count. A package word is not a base unit — the caller
    records it through the ingredient's package. An unknown word raises
    ``InputUnitError``, whose message names the units the form accepts.
    """
    measure, pack = parse_input_unit(unit)
    if pack:
        raise NeedsPackage(pack)
    if measure is None:
        return "count", None
    return base_unit_for(measure), unit.strip() or None


MAX_SERVINGS = 50


def _default_servings(recipe: Recipe) -> int:
    return recipe.last_servings or recipe.servings or 4
