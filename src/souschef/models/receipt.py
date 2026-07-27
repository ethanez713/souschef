"""Receipt uploads and their extracted line items."""

from __future__ import annotations

import enum
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from souschef.models.base import Base, OwnedMixin, TimestampMixin

if TYPE_CHECKING:
    from souschef.models.ingredient import CanonicalIngredient


class ReceiptStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    OCR_RUNNING = "ocr_running"
    NEEDS_REVIEW = "needs_review"  # parsed; waiting on the user to confirm matches
    APPLIED = "applied"  # confirmed and written to the ledger
    FAILED = "failed"


class OcrEngine(enum.StrEnum):
    RAPIDOCR = "rapidocr"  # self-hosted, default
    VLM = "vlm"  # cloud fallback, only after explicit per-receipt consent
    MANUAL = "manual"


class Receipt(Base, OwnedMixin, TimestampMixin):
    __tablename__ = "receipt"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Path relative to the receipt directory. Images stay on our own disk; they
    # are transaction data revealing spend, location and habits.
    #
    # The filename is content-addressed, so two users who upload byte-identical
    # images share one file. That leaks nothing -- each already holds those
    # bytes -- but it does mean the de-duplication lookup on `image_sha256` has
    # to be scoped to the owner, or one user's upload would hand back the
    # other's receipt row.
    image_path: Mapped[str] = mapped_column(String(500))
    image_sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[ReceiptStatus] = mapped_column(
        Enum(ReceiptStatus, native_enum=False, length=14),
        default=ReceiptStatus.UPLOADED,
        index=True,
    )
    engine: Mapped[OcrEngine] = mapped_column(
        Enum(OcrEngine, native_enum=False, length=10), default=OcrEngine.RAPIDOCR
    )

    store_name: Mapped[str | None] = mapped_column(String(200))
    purchased_on: Mapped[date | None] = mapped_column(Date)
    total_amount: Mapped[float | None] = mapped_column(Float)

    # Mean OCR confidence; drives whether the VLM fallback gets offered.
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    # Raw engine output, kept so parsing can be re-run without re-OCRing.
    raw_ocr: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(String(500))

    lines: Mapped[list[ReceiptLine]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="ReceiptLine.position"
    )

    def __repr__(self) -> str:
        return f"<Receipt {self.id} {self.store_name!r} {self.status}>"


class ReceiptLine(Base):
    """One extracted line, pre-confirmation.

    Nothing here touches the pantry until the user confirms it. Receipt OCR on
    thermal paper is unreliable enough that silently applying it would corrupt
    inventory faster than manual entry ever could.
    """

    __tablename__ = "receipt_line"
    __table_args__ = (Index("ix_receipt_line_receipt", "receipt_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)

    raw_text: Mapped[str] = mapped_column(String(300))
    ocr_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    parsed_description: Mapped[str | None] = mapped_column(String(200))
    parsed_quantity: Mapped[float | None] = mapped_column(Float)
    parsed_unit: Mapped[str | None] = mapped_column(String(32))
    parsed_price: Mapped[float | None] = mapped_column(Float)

    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_ingredient.id", ondelete="SET NULL"), index=True
    )
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Non-food lines (bags, deposits, subtotals) that shouldn't reach the pantry.
    is_ignored: Mapped[bool] = mapped_column(default=False)
    is_confirmed: Mapped[bool] = mapped_column(default=False)

    receipt: Mapped[Receipt] = relationship(back_populates="lines")
    ingredient: Mapped[CanonicalIngredient | None] = relationship()

    def __repr__(self) -> str:
        return f"<ReceiptLine {self.raw_text!r}>"
