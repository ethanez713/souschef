"""Contrast and type-scale floors for the design tokens (ds.css) and the app override (app.css)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "src" / "souschef" / "static"
MIN_CONTRAST = 4.5
MIN_PAIRS = 5

LIGHT_SELECTOR = ':root,[data-theme="light"]'
DARK_SELECTOR = ':root[data-theme="dark"]'


def _blocks(css: str) -> list[tuple[str, str]]:
    """Top-level ``selector { body }`` rules; at-rules are skipped whole."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, depth, start, selector = [], 0, 0, ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                selector, start = css[start:index].strip(), index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                if not selector.startswith("@"):
                    rules.append((selector, css[start:index]))
                start = index + 1
    return rules


def _declarations(body: str) -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);?", body)
    }


def _theme_tokens(css: str, dark: bool) -> dict[str, str]:
    tokens: dict[str, str] = {}
    wanted = (LIGHT_SELECTOR, DARK_SELECTOR) if dark else (LIGHT_SELECTOR,)
    for selector, body in _blocks(css):
        if selector.startswith(wanted):
            tokens |= _declarations(body)
    return tokens


def _resolve(tokens: dict[str, str], name: str, depth: int = 0) -> str:
    value = tokens[name]
    match = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if match and depth < 10:
        return _resolve(tokens, match.group(1), depth + 1)
    return value


def _luminance(hex_color: str) -> float:
    digits = hex_color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    channels = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    a, b = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


FILLS = {
    "--on-accent": ["--accent", "--accent-hover", "--accent-press"],
    "--on-danger": ["--danger"],
    "--on-accent-soft": ["--surface-accent-soft"],
}
GROUNDS = ["--bg-app", "--surface-card"]


def _pairs(tokens: dict[str, str]) -> list[tuple[str, str]]:
    pairs = [(on, fill) for on, fills in FILLS.items() for fill in fills]
    texts = [
        name for name in tokens if name.startswith("--text-") and name != "--text-inverse"
    ]
    pairs += [(text, ground) for text in sorted(texts) for ground in GROUNDS]
    return pairs


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_foreground_tokens_clear_contrast_on_their_grounds(dark: bool) -> None:
    tokens = _theme_tokens((STATIC / "ds.css").read_text(), dark)
    pairs = _pairs(tokens)
    assert len(pairs) >= MIN_PAIRS, "token rename left too few pairs to check"

    failures = []
    for foreground, background in pairs:
        ratio = _contrast(_resolve(tokens, foreground), _resolve(tokens, background))
        if ratio < MIN_CONTRAST:
            failures.append(f"{foreground} on {background}: {ratio:.2f}")
    assert not failures, failures


def test_app_type_scale_stays_readable() -> None:
    declared = {
        name: float(size)
        for name, size in re.findall(
            r"(--fs-[\w-]+)\s*:\s*(\d+(?:\.\d+)?)px", (STATIC / "app.css").read_text()
        )
    }
    assert declared["--fs-body"] == 16
    assert declared, "app.css declares no --fs-* sizes"
    assert all(size >= 13 for size in declared.values()), declared
