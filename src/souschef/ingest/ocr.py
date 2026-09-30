"""Optical character recognition for receipts.

Self-hosted by default. Grocery receipts are transaction data -- they reveal
spend, location and habits -- so they are processed on our own machine, and the
cloud fallback is only ever reached after an explicit per-receipt confirmation.

Two properties of real OCR output shape this module, both learned by running the
engine on an actual receipt rather than assumed:

**The engine returns fragments, not lines.** A single receipt row comes back as
several boxes ("GV", "EVOO 25.5Z", "8.47 F"). Handing those to the matcher
individually is useless, so boxes are reassembled into rows using their
geometry.

**Character confusion is confident.** Zero-for-O substitutions ("EV00",
"ONI0N", "T0MAT0ES") arrive at 0.96+ confidence, so a confidence threshold will
not catch them. They need explicit repair.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from souschef.logconfig import get_logger

log = get_logger("souschef.ocr")

# Below this, the scan is poor enough that the review screen should say so.
LOW_CONFIDENCE = 0.75

# Digit/letter pairs that thermal-print OCR confuses. Restricted to the two
# substitutions that are both common and low-risk: zero-for-O dominates real
# receipts, and one-for-l is close behind. Adding 5/S, 8/B or 2/Z was tried and
# reverted -- they fire on genuine quantities ("2.34 lb" became "z.34 lb") far
# more often than they fix a word.
_CONFUSIONS = str.maketrans({"0": "o", "1": "l"})

_HAS_LETTER = re.compile(r"[a-zA-Z]")
_HAS_DIGIT = re.compile(r"\d")


@dataclass
class OcrBox:
    text: str
    confidence: float
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def y_center(self) -> float:
        return (self.y_min + self.y_max) / 2

    @property
    def height(self) -> float:
        return self.y_max - self.y_min


@dataclass
class OcrLine:
    text: str
    confidence: float
    y_center: float
    boxes: list[OcrBox] = field(default_factory=list)


@dataclass
class OcrResult:
    lines: list[OcrLine] = field(default_factory=list)
    engine: str = "rapidocr"

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class OcrUnavailable(RuntimeError):
    """The OCR engine could not be loaded."""


def repair_confusions(token: str) -> str:
    """Undo digit-for-letter OCR errors inside a word-like token.

    Deliberately conservative, and intended to run *after* prices, sizes and
    item codes have been stripped -- corrupting a real quantity is worse than
    leaving a garbled word for the fuzzy matcher to cope with. A token only
    qualifies if letters clearly dominate and it carries no decimal point,
    which rules out weights like "2.34" and codes like "412".
    """
    if not token or not _HAS_LETTER.search(token) or not _HAS_DIGIT.search(token):
        return token
    if "." in token or "/" in token:
        return token
    letters = sum(character.isalpha() for character in token)
    digits = sum(character.isdigit() for character in token)
    # Ties are repaired, not skipped: "EV00" is half digits and is exactly the
    # case this exists for. Only a clear digit majority means "quantity".
    if letters < digits:
        return token
    return token.translate(_CONFUSIONS)


def repair_line(text: str) -> str:
    return " ".join(repair_confusions(token) for token in text.split())


def group_boxes_into_lines(boxes: list[OcrBox], *, tolerance: float = 0.6) -> list[OcrLine]:
    """Reassemble fragment boxes into receipt rows using vertical position.

    Boxes whose vertical centres fall within ``tolerance`` of a typical
    character height are treated as one row and joined left to right. Without
    this the matcher sees "GV" and "EVOO 25.5Z" as unrelated strings.
    """
    if not boxes:
        return []

    heights = sorted(box.height for box in boxes if box.height > 0)
    typical = heights[len(heights) // 2] if heights else 10.0
    threshold = max(typical * tolerance, 1.0)

    ordered = sorted(boxes, key=lambda b: (b.y_center, b.x_min))
    rows: list[list[OcrBox]] = []

    for box in ordered:
        if rows and abs(box.y_center - rows[-1][0].y_center) <= threshold:
            rows[-1].append(box)
        else:
            rows.append([box])

    lines: list[OcrLine] = []
    for row in rows:
        row.sort(key=lambda b: b.x_min)
        text = " ".join(box.text.strip() for box in row if box.text.strip())
        if not text:
            continue
        # Text is kept verbatim. Confusion repair happens downstream, once
        # prices and sizes have been stripped, so quantities survive intact.
        lines.append(
            OcrLine(
                text=text,
                confidence=min(box.confidence for box in row),
                y_center=row[0].y_center,
                boxes=row,
            )
        )
    return lines


def _to_boxes(raw_result: list) -> list[OcrBox]:
    """Normalize RapidOCR's ``[polygon, text, confidence]`` triples."""
    boxes: list[OcrBox] = []
    for entry in raw_result or []:
        try:
            polygon, text, confidence = entry[0], entry[1], entry[2]
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
        except (IndexError, TypeError, ValueError):
            continue
        if not text or not str(text).strip():
            continue
        boxes.append(
            OcrBox(
                text=str(text),
                confidence=float(confidence),
                x_min=min(xs), y_min=min(ys), x_max=max(xs), y_max=max(ys),
            )
        )
    return boxes


_engine = None


def _get_engine():  # noqa: ANN202
    """Load RapidOCR lazily.

    Model load costs a second and a couple of hundred MB, which shouldn't be
    paid by a process that never scans a receipt.
    """
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            log.error("RapidOCR is not installed; receipt scanning is unavailable")
            raise OcrUnavailable(
                "rapidocr-onnxruntime is not installed; receipt scanning is unavailable."
            ) from exc
        started = time.perf_counter()
        _engine = RapidOCR()
        # Worth logging: the first scan after a restart pays this, and it is the
        # usual explanation for one slow request that never recurs.
        log.info("OCR models loaded in %.1fs", time.perf_counter() - started)
    return _engine


# 180 is omitted: an upside-down receipt is rare, and rotating it reads the
# show-through on the back as if it were the front.
_ORIENTATIONS = (0, 90, 270)


def _orientation_key(result: OcrResult) -> tuple[float, int, int]:
    characters = sum(len(line.text) for line in result.lines)
    return (round(result.mean_confidence, 4), len(result.lines), characters)


def choose_orientation(candidates: list[tuple[int, OcrResult]]) -> tuple[int, OcrResult]:
    """Pick the rotation whose text is the most confident, then the fullest.

    ``candidates`` is ``(degrees, result)`` pairs. The caller decides which
    angles exist; this only ranks results it is given.
    """
    if not candidates:
        raise ValueError("choose_orientation requires at least one result")
    return max(candidates, key=lambda item: _orientation_key(item[1]))


def _fill_color(image: object) -> int | tuple[int, ...]:
    mode = getattr(image, "mode", "RGB")
    if mode == "L":
        return 255
    if mode == "RGBA":
        return (255, 255, 255, 255)
    return (255, 255, 255)


def _read_frame(engine: object, image: object) -> OcrResult:
    raw_result, _timing = engine(image)  # type: ignore[operator]
    return OcrResult(lines=group_boxes_into_lines(_to_boxes(raw_result)), engine="rapidocr")


def run_ocr(image_path: str | Path) -> OcrResult:
    """Extract text rows from a receipt image.

    A phone photo of a long receipt is often sideways. Each of 0, 90 and 270
    degrees is read, and the orientation with the best text is kept.
    """
    path = Path(image_path)
    if not path.is_file():
        log.error("Receipt image missing: %s", path)
        raise OcrUnavailable(f"No such image: {path}")

    from PIL import Image

    engine = _get_engine()
    image = Image.open(path)
    image.load()
    started = time.perf_counter()
    try:
        candidates: list[tuple[int, OcrResult]] = []
        for angle in _ORIENTATIONS:
            frame = image if angle == 0 else image.rotate(
                angle, expand=True, fillcolor=_fill_color(image)
            )
            candidates.append((angle, _read_frame(engine, frame)))
    except Exception as exc:
        log.exception("OCR failed on %s", path.name)
        raise OcrUnavailable(f"OCR failed on {path.name}: {exc}") from exc

    angle, result = choose_orientation(candidates)
    lines = result.lines
    if angle:
        log.info("OCR rotated %s by %d degrees", path.name, angle)

    log.info(
        "OCR %s: %d lines, confidence %.2f, %.1fs",
        path.name, len(lines), result.mean_confidence,
        time.perf_counter() - started,
    )
    if not lines:
        log.warning("OCR found no text in %s -- likely a blurry or blank photo", path.name)
    elif result.mean_confidence < LOW_CONFIDENCE:
        log.warning(
            "Low-confidence scan of %s (%.2f); matches will need careful review",
            path.name, result.mean_confidence,
        )
    return result
