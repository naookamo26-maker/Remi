"""app.css のカラートークンのコントラスト比を確認する(設計書 §9.2)。"""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "src" / "remi" / "web" / "css" / "app.css").read_text(encoding="utf-8")

PAIRS = [
    ("text", "window"), ("text", "surface"), ("text", "surface-alt"), ("text", "selection"),
    ("text-muted", "window"), ("text-muted", "surface"), ("text-muted", "surface-alt"),
    ("on-accent", "accent"), ("link", "window"), ("link", "surface-alt"),
    ("text", "bubble-other"), ("text", "bubble-mine"), ("text", "bubble-private"), ("text", "code-bg"),
    ("mention-fg", "mention-bg"), ("danger", "window"), ("success", "window"), ("warning-fg", "warning-bg"),
]


def tokens(selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", block))


LIGHT = tokens(":root")
DARK = tokens(":root.theme-dark")


def luminance(hex_: str) -> float:
    vals = []
    for i in (1, 3, 5):
        c = int(hex_[i:i + 2], 16) / 255
        vals.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = vals
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_same_tokens_in_both_themes():
    assert set(LIGHT) == set(DARK)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_contrast_aa(theme, fg, bg):
    t = LIGHT if theme == "light" else DARK
    ratio = contrast(t[fg], t[bg])
    assert ratio >= 4.5, f"{theme}: {fg} on {bg} = {ratio:.2f}"
