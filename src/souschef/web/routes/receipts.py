"""Routes: receipts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.matching import IngredientMatcher
from souschef.db import get_db
from souschef.ingest.receipt_service import (
    ReceiptError,
    apply_receipt,
    create_receipt,
    image_key,
    scan_receipt,
)
from souschef.models import CanonicalIngredient, Receipt, ReceiptLine, ReceiptStatus, User
from souschef.models.ingredient import AliasSource
from souschef.planning.shopping import ingredients_stocked_since
from souschef.storage import StorageError, get_storage
from souschef.web import auth
from souschef.web.common import (
    _owned,
    _render,
)

router = APIRouter()


LIST_STOCK_NOTE = "Already added from the shopping list, so this line stays unticked."


@router.get("/receipts", response_class=HTMLResponse)
def receipts_page(
    request: Request,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    receipts = db.scalars(
        select(Receipt)
        .where(Receipt.user_id == user.id)
        .order_by(Receipt.created_at.desc())
        .limit(20)
    ).all()
    return _render(request, "receipts.html", receipts=receipts)


STOCK_WINDOW_DAYS = 3


def _untick_stocked_lines(db: Session, user: User, receipt: Receipt) -> dict[int, str]:
    """Drop ticks on lines whose ingredient was bought from the list for this shop.

    The window runs from a few days before the receipt's purchase date through
    the day it was uploaded, so an older shop never unticks a newer receipt.
    """
    if receipt.status == ReceiptStatus.APPLIED:
        return {}
    uploaded = receipt.created_at
    if uploaded.tzinfo is None:
        uploaded = uploaded.replace(tzinfo=UTC)
    shopped_on = receipt.purchased_on or uploaded.date()
    window_start = datetime.combine(
        shopped_on - timedelta(days=STOCK_WINDOW_DAYS), datetime.min.time(), tzinfo=UTC
    )
    stocked = ingredients_stocked_since(db, user.id, window_start, uploaded)
    notes: dict[int, str] = {}
    for line in receipt.lines:
        if line.is_ignored or line.ingredient_id is None:
            continue
        if line.ingredient_id not in stocked:
            continue
        line.is_confirmed = False
        notes[line.id] = LIST_STOCK_NOTE
    if notes:
        db.flush()
    return notes


def _review_receipt(
    request: Request,
    db: Session,
    receipt: Receipt,
    *,
    error: str = "",
    status_code: int = 200,
    user: User | None = None,
) -> HTMLResponse:
    owner = user or getattr(request.state, "user", None)
    list_notes: dict[int, str] = {}
    if owner is not None and receipt.status != ReceiptStatus.APPLIED:
        list_notes = _untick_stocked_lines(db, owner, receipt)
    ingredients = db.scalars(select(CanonicalIngredient).order_by(CanonicalIngredient.name)).all()
    visible = [line for line in receipt.lines if not line.is_ignored]
    ignored = [line for line in receipt.lines if line.is_ignored]
    return _render(
        request,
        "receipt_review.html",
        status_code=status_code,
        receipt=receipt,
        lines=visible,
        ignored=ignored,
        ingredients=ingredients,
        error=error,
        list_notes=list_notes,
        already=request.query_params.get("already") == "1",
    )


def _save_receipt_lines(db: Session, receipt: Receipt, form) -> None:  # noqa: ANN001
    """Write the review form onto the lines before applying them.

    Apply used to submit a form that contained none of the line fields, so
    unticked-then-ticked rows were discarded and the receipt locked anyway.
    """
    if receipt.status == ReceiptStatus.APPLIED:
        raise ReceiptError("This receipt has already been applied.")

    planned: list[tuple[ReceiptLine, int | None, float | None, str, bool]] = []
    for line in receipt.lines:
        if line.is_ignored:
            continue
        ingredient_key = f"ingredient_{line.id}"
        quantity_key = f"quantity_{line.id}"
        unit_key = f"unit_{line.id}"
        if ingredient_key not in form and quantity_key not in form and unit_key not in form:
            continue
        ingredient_raw = str(form.get(ingredient_key) or "").strip()
        quantity_raw = str(form.get(quantity_key) or "").strip()
        unit_raw = str(form.get(unit_key) or "").strip()
        ingredient_id = None
        if ingredient_raw:
            try:
                ingredient_id = int(ingredient_raw)
            except ValueError as exc:
                raise ReceiptError(f"'{line.raw_text}' has no ingredient.") from exc
        quantity = None
        if quantity_raw:
            try:
                quantity = float(quantity_raw)
            except ValueError as exc:
                raise ReceiptError(f"Amount for '{line.raw_text}' isn't a number.") from exc
            if quantity <= 0:
                raise ReceiptError(f"Amount for '{line.raw_text}' has to be more than zero.")
        confirmed = form.get(f"confirmed_{line.id}") in ("on", "true", "1")
        if ingredient_id is None:
            confirmed = False
        elif line.ingredient_id is None:
            # Naming an unmatched line is the user vouching for it; leaving the
            # box unticked silently dropped the purchase on apply.
            confirmed = True
        planned.append((line, ingredient_id, quantity, unit_raw, confirmed))

    matcher = IngredientMatcher(db)
    for line, ingredient_id, quantity, unit_raw, confirmed in planned:
        if ingredient_id is not None and ingredient_id != line.ingredient_id:
            matcher.learn(line.raw_text, ingredient_id, source=AliasSource.MANUAL, is_receipt=True)
            line.match_confidence = 1.0
        line.ingredient_id = ingredient_id
        if quantity is not None:
            line.parsed_quantity = quantity
        if unit_raw:
            line.parsed_unit = unit_raw
        line.is_confirmed = confirmed
    db.flush()


@router.post("/receipts/upload")
async def upload_receipt(
    request: Request,
    image: UploadFile,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    data = await image.read()
    try:
        receipt = create_receipt(db, user.id, data, image.filename or "receipt.png")
        # Re-running OCR on an applied receipt wiped the confirmations and
        # reopened it. A photo that still needs review can be retried.
        if receipt.status == ReceiptStatus.APPLIED:
            return RedirectResponse(f"/receipts/{receipt.id}?already=1", status_code=303)
        scan_receipt(db, receipt)
    except ReceiptError as exc:
        receipts = db.scalars(
            select(Receipt)
            .where(Receipt.user_id == user.id)
            .order_by(Receipt.created_at.desc())
            .limit(20)
        ).all()
        return _render(request, "receipts.html", status_code=400, receipts=receipts, error=str(exc))
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


@router.get("/receipts/{receipt_id}/image")
def receipt_image(
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    """The photo, and only to the account that uploaded it."""
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    try:
        data = get_storage().read(image_key(receipt.image_path))
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="No such receipt") from exc
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/receipts/{receipt_id}", response_class=HTMLResponse)
def receipt_review(
    request: Request,
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    return _review_receipt(request, db, receipt)


@router.post("/receipts/{receipt_id}/line/{line_id}")
def update_receipt_line(
    request: Request,
    receipt_id: int,
    line_id: int,
    ingredient_id: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    confirmed: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    # Ownership is checked on the receipt, then the line is checked to belong to
    # that receipt -- verifying only one of the two would let a line from
    # someone else's receipt be edited through a receipt id you do own.
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    if receipt.status == ReceiptStatus.APPLIED:
        return _review_receipt(
            request,
            db,
            receipt,
            error="This receipt has already been applied.",
            status_code=400,
        )
    line = db.get(ReceiptLine, line_id)
    if line is None or line.receipt_id != receipt_id:
        raise HTTPException(status_code=404, detail="No such line")

    if ingredient_id.strip():
        new_id = int(ingredient_id)
        # A manual correction is the strongest signal there is, so teach it.
        if new_id != line.ingredient_id:
            IngredientMatcher(db).learn(
                line.raw_text, new_id, source=AliasSource.MANUAL, is_receipt=True
            )
        line.ingredient_id = new_id
        line.match_confidence = 1.0

    if quantity.strip():
        try:
            parsed = float(quantity)
        except ValueError:
            return _review_receipt(
                request, db, receipt, error="Amount isn't a number.", status_code=400
            )
        if parsed <= 0:
            return _review_receipt(
                request,
                db,
                receipt,
                error="Amount has to be more than zero.",
                status_code=400,
            )
        line.parsed_quantity = parsed
    if unit.strip():
        line.parsed_unit = unit
    line.is_confirmed = confirmed == "on" and line.ingredient_id is not None
    db.flush()
    return RedirectResponse(f"/receipts/{receipt_id}", status_code=303)


def _added_label(line: ReceiptLine, ingredient: CanonicalIngredient) -> str:
    if not line.parsed_quantity:
        return ingredient.name
    amount = line.parsed_quantity
    text = str(int(round(amount))) if abs(amount - round(amount)) < 0.05 else f"{amount:g}"
    unit = (line.parsed_unit or "").strip()
    garlic = ingredient.name.casefold() == "garlic"
    if garlic and unit.casefold() in {"", "count", "clove", "cloves"}:
        word = "clove" if text == "1" else "cloves"
        return f"{ingredient.name} ({text} {word})"
    if unit and unit.casefold() != "count":
        return f"{ingredient.name} ({text} {unit})"
    if unit.casefold() == "count":
        return f"{ingredient.name} ({text} count)"
    return f"{ingredient.name} ({text})"


def _added_labels(db: Session, receipt: Receipt, warnings: list[str]) -> list[str]:
    failed = set()
    for warning in warnings:
        raw, sep, _rest = warning.partition(":")
        if sep:
            failed.add(raw)
    labels = []
    for line in receipt.lines:
        if line.is_ignored or not line.is_confirmed or line.ingredient_id is None:
            continue
        if line.raw_text in failed:
            continue
        ingredient = db.get(CanonicalIngredient, line.ingredient_id)
        if ingredient is None:
            continue
        labels.append(_added_label(line, ingredient))
    return labels


@router.get("/receipts/{receipt_id}/apply")
def apply_receipt_get(
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """A refresh after applying is a GET. The form lives on the review page."""
    _owned(db, Receipt, receipt_id, user, "receipt")
    return RedirectResponse(f"/receipts/{receipt_id}", status_code=303)


@router.post("/receipts/{receipt_id}/apply")
async def apply_receipt_route(
    request: Request,
    receipt_id: int,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
) -> Response:
    receipt = _owned(db, Receipt, receipt_id, user, "receipt")
    form = await request.form()
    try:
        _save_receipt_lines(db, receipt, form)
        added, warnings = apply_receipt(db, receipt)
    except ReceiptError as exc:
        return _review_receipt(request, db, receipt, error=str(exc), status_code=400)

    if added == 0:
        return _review_receipt(
            request,
            db,
            receipt,
            error=warnings[-1] if warnings else "Tick a line before applying.",
            status_code=400,
        )
    return _render(
        request,
        "receipt_applied.html",
        receipt=receipt,
        warnings=warnings,
        added=added,
        added_labels=_added_labels(db, receipt, warnings),
    )
