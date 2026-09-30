"""Receipt lifecycle: upload, scan, review, apply.

The staging step is the point. OCR proposals land in ``receipt_line`` rows and
sit there until confirmed; only :func:`apply_receipt` touches the pantry. That
keeps a bad scan from corrupting inventory, and makes every confirmation a
chance to teach the matcher.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.core.inventory import record_event
from souschef.core.matching import IngredientMatcher
from souschef.core.units import UnitConversionError
from souschef.ingest.ocr import OcrUnavailable, run_ocr
from souschef.ingest.receipt import (
    detect_store,
    detect_total,
    parse_receipt,
    redact_receipt_text,
)
from souschef.logconfig import get_logger
from souschef.models import (
    CanonicalIngredient,
    EventReason,
    Receipt,
    ReceiptLine,
    ReceiptStatus,
)
from souschef.models.ingredient import AliasSource
from souschef.models.receipt import OcrEngine
from souschef.storage import StorageError, get_storage

log = get_logger("souschef.receipt")

# Below this mean OCR confidence, offer the cloud fallback. Tuned so a clean
# phone photo never triggers it -- the prompt should mean "this scan is genuinely
# bad", not appear on every receipt.
VLM_FALLBACK_THRESHOLD = 0.75

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024

# The database stores the bare filename, so the prefix lives here alone and a
# stored receipt row stays valid whatever the storage backend is.
RECEIPT_PREFIX = "receipts"


def image_key(name: str) -> str:
    return f"{RECEIPT_PREFIX}/{name}"


@dataclass
class ScanSummary:
    receipt: Receipt
    item_count: int
    auto_matched: int
    needs_review: int
    mean_confidence: float
    suggest_vlm_fallback: bool


class ReceiptError(RuntimeError):
    """Something went wrong that the user should see."""


def store_upload(data: bytes, filename: str) -> tuple[str, str]:
    """Save an uploaded image, stripping metadata, and return its name and hash.

    EXIF is discarded rather than stored: phone photos carry GPS coordinates,
    and a receipt image tagged with your home address is a needless liability
    for a feature that only needs the pixels.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ReceiptError(f"Unsupported image type {suffix!r}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ReceiptError("Image is too large (limit 15 MB)")

    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest[:16]}.png"

    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            # Re-encoding through a fresh image drops every metadata block,
            # including GPS, rather than trusting a targeted EXIF removal.
            clean = Image.new(image.mode, image.size)
            clean.putdata(list(image.getdata()))
            buffer = io.BytesIO()
            clean.save(buffer, format="PNG")
    except Exception as exc:  # noqa: BLE001 - any decode failure is user-facing
        raise ReceiptError(f"Could not read that image: {exc}") from exc

    get_storage().write(image_key(name), buffer.getvalue())
    return name, digest


def create_receipt(session: Session, user_id: int, data: bytes, filename: str) -> Receipt:
    name, digest = store_upload(data, filename)

    # Scoped to the owner: the image file is content-addressed and therefore
    # shared, but the receipt row must not be. An unscoped lookup would hand a
    # second user the first user's receipt, review state and all.
    existing = session.scalar(
        select(Receipt).where(Receipt.user_id == user_id, Receipt.image_sha256 == digest)
    )
    if existing is not None:
        return existing

    receipt = Receipt(
        user_id=user_id,
        image_path=name,
        image_sha256=digest,
        status=ReceiptStatus.UPLOADED,
    )
    session.add(receipt)
    session.flush()
    return receipt


def scan_receipt(session: Session, receipt: Receipt) -> ScanSummary:
    """Run OCR and stage the proposed line items for review."""
    receipt.status = ReceiptStatus.OCR_RUNNING
    session.flush()

    try:
        with get_storage().local_path(image_key(receipt.image_path)) as path:
            result = run_ocr(path)
    except (OcrUnavailable, StorageError) as exc:
        receipt.status = ReceiptStatus.FAILED
        receipt.error_message = str(exc)
        session.flush()
        raise ReceiptError(str(exc)) from exc

    # Card numbers are not needed to review a match, and the raw text is what
    # the review page prints.
    receipt.raw_ocr = redact_receipt_text(result.text)
    receipt.ocr_confidence = result.mean_confidence
    receipt.engine = OcrEngine.RAPIDOCR
    receipt.store_name = detect_store(result.lines)
    receipt.total_amount = detect_total(result.lines)

    receipt.lines.clear()
    session.flush()

    parsed_lines = parse_receipt(result, session)

    auto = 0
    review = 0
    items = 0
    for position, parsed in enumerate(parsed_lines):
        match = parsed.match
        if not parsed.is_noise:
            items += 1
            if match and match.is_auto:
                auto += 1
            else:
                review += 1

        session.add(
            ReceiptLine(
                receipt_id=receipt.id,
                position=position,
                raw_text=redact_receipt_text(parsed.raw_text)[:300],
                ocr_confidence=parsed.ocr_confidence,
                parsed_description=parsed.description[:200] or None,
                parsed_quantity=parsed.quantity,
                parsed_unit=parsed.unit,
                parsed_price=parsed.price,
                ingredient_id=match.ingredient_id if match and match.is_match else None,
                match_confidence=match.confidence if match else 0.0,
                is_ignored=parsed.is_noise,
                # Nothing is pre-confirmed: a confident match is still a
                # proposal until a human agrees with it.
                is_confirmed=False,
            )
        )

    receipt.status = ReceiptStatus.NEEDS_REVIEW
    session.flush()
    session.expire(receipt, ["lines"])

    log.info(
        "Receipt %d (%s): %d items, %d auto-matched, %d need review",
        receipt.id, receipt.store_name or "unknown store", items, auto, review,
    )
    if items == 0:
        log.warning(
            "Receipt %d produced no line items -- every row classified as noise. "
            "Check the photo, or the store's layout may need new noise rules.",
            receipt.id,
        )
    elif review > auto:
        log.warning(
            "Receipt %d: most lines (%d of %d) need manual review; the alias table "
            "has not learned this store yet",
            receipt.id, review, items,
        )

    return ScanSummary(
        receipt=receipt,
        item_count=items,
        auto_matched=auto,
        needs_review=review,
        mean_confidence=result.mean_confidence,
        suggest_vlm_fallback=result.mean_confidence < VLM_FALLBACK_THRESHOLD,
    )


def apply_receipt(
    session: Session,
    receipt: Receipt,
    *,
    purchased_on: date | None = None,
) -> tuple[int, list[str]]:
    """Write confirmed lines into the pantry ledger.

    Returns how many lines were written, and human-readable warnings for lines
    that could not be applied. Confirming nothing does not mark the receipt
    applied: that lock is permanent, and it used to throw away the review.
    """
    if receipt.status == ReceiptStatus.APPLIED:
        raise ReceiptError("This receipt has already been applied.")

    matcher = IngredientMatcher(session)
    warnings: list[str] = []
    applied = 0

    for line in receipt.lines:
        if line.is_ignored or not line.is_confirmed or line.ingredient_id is None:
            continue

        ingredient = session.get(CanonicalIngredient, line.ingredient_id)
        if ingredient is None:
            warnings.append(f"{line.raw_text}: ingredient no longer exists")
            continue

        quantity, unit = _resolve_quantity(line, ingredient)
        if quantity is None:
            log.warning(
                "Receipt %d line %d (%s): no quantity and no package size for %r",
                receipt.id, line.id, line.raw_text[:60], ingredient.name,
            )
            warnings.append(
                f"{line.raw_text}: no quantity could be determined -- set one manually"
            )
            continue

        try:
            record_event(
                session,
                # Taken from the receipt itself rather than passed in: the owner
                # of the receipt is by definition whose pantry it credits, and
                # deriving it here makes a mismatch unrepresentable.
                receipt.user_id,
                ingredient,
                reason=EventReason.PURCHASE,
                delta=quantity,
                unit=unit,
                event_confidence=min(1.0, line.match_confidence or 0.5),
                source_ref=f"receipt:{receipt.id}",
                note=line.parsed_description or line.raw_text[:120],
                occurred_at=(
                    datetime.combine(purchased_on, datetime.min.time(), tzinfo=UTC)
                    if purchased_on
                    else None
                ),
            )
        except UnitConversionError as exc:
            # Almost always a missing density or per-item weight in the catalog,
            # which is a data gap worth fixing rather than a transient error.
            log.warning(
                "Receipt %d: cannot convert %s %s of %r -- %s",
                receipt.id, quantity, unit, ingredient.name, exc,
            )
            warnings.append(f"{line.raw_text}: {exc}")
            continue

        applied += 1

        # Every confirmation teaches the matcher this store's shorthand, which
        # is what makes the next receipt cheaper to review.
        if line.parsed_description:
            matcher.learn(
                line.raw_text,
                line.ingredient_id,
                source=AliasSource.RECEIPT,
                is_receipt=True,
            )

    log.info(
        "Receipt %d applied: %d lines written to the pantry, %d warnings",
        receipt.id, applied, len(warnings),
    )
    if applied == 0:
        warnings.append("No lines were confirmed, so nothing was added to the pantry.")
        return 0, warnings

    if purchased_on:
        receipt.purchased_on = purchased_on
    receipt.status = ReceiptStatus.APPLIED
    session.flush()
    return applied, warnings


def _resolve_quantity(
    line: ReceiptLine, ingredient: CanonicalIngredient
) -> tuple[float | None, str | None]:
    """Work out how much was bought, falling back to one package.

    A receipt frequently states no size at all ("GRND BF 80/20  6.49"), because
    the package is implied. Assuming one standard package is far better than
    discarding the purchase, and the user can correct it.
    """
    if line.parsed_quantity and line.parsed_unit:
        return line.parsed_quantity, line.parsed_unit
    if line.parsed_quantity and ingredient.base_unit == "count":
        return line.parsed_quantity, "count"
    if ingredient.package_qty and ingredient.package_unit:
        return ingredient.package_qty, ingredient.package_unit
    return None, None
