"""Regenerate src/souschef/static/ds.css from the Ethan design system's token files."""

from pathlib import Path

TOKENS = Path.home() / "ethan_design_system" / "tokens"
ORDER = [
    "colors.css", "colors-dark.css", "typography.css", "spacing.css",
    "radius.css", "elevation.css", "motion.css", "base.css",
]
HEADER = (
    "/* Ethan design system tokens, concatenated from ethan_design_system/tokens/ "
    "(fonts excluded: see fonts/fonts.css). Values change in the design system, not here. */\n"
)
TARGET = Path(__file__).resolve().parent.parent / "src" / "souschef" / "static" / "ds.css"

TARGET.write_text(HEADER + "\n".join((TOKENS / name).read_text() for name in ORDER))
