"""Appearance tokens, glyph sets and the ASCII fallback for the prototype."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from prompt_toolkit.styles import Style

THEMES = ("dark", "light", "terminal")
THEME_LABELS = {"dark": "Dark", "light": "Light", "terminal": "Terminal default"}

DARK = {
    "canvas": "#15191E",
    "panel": "#1E252D",
    "text": "#E6EDF3",
    "muted": "#A8B3BF",
    "border": "#526170",
    "focus": "#77D8C3",
    "accent": "#77D8C3",
    "attention": "#F0C674",
    "error": "#F28B82",
    "selected": "#293E4B",
}

LIGHT = {
    "canvas": "#FAFAF7",
    "panel": "#FFFFFF",
    "text": "#1F2933",
    "muted": "#52606D",
    "border": "#9AA5B1",
    "focus": "#0B7285",
    "accent": "#0B7285",
    "attention": "#8A5A00",
    "error": "#B42318",
    "selected": "#D9EEF2",
}


def make_style(name: str) -> Style:
    """Build the prompt_toolkit style for a theme name.

    ``terminal`` uses no colours at all (terminal defaults, no-colour friendly)
    and relies on bold/reverse/underline so labels and markers stay distinct.
    """
    if name == "terminal":
        return Style.from_dict(
            {
                "title": "bold",
                "selected": "reverse",
                "button.focus": "reverse bold",
                "button.disabled": "italic",
                "border.focus": "bold",
                "chip": "underline",
                "user": "bold",
                "assistant": "bold",
                "bold": "bold",
                "failed": "bold",
                "attention": "bold",
                "error": "bold",
                "code": "italic",
            }
        )
    t = DARK if name == "dark" else LIGHT
    return Style.from_dict(
        {
            "canvas": f"bg:{t['canvas']} {t['text']}",
            "panel": f"bg:{t['panel']} {t['text']}",
            "header": f"bg:{t['panel']} {t['text']}",
            "footer": f"bg:{t['panel']} {t['muted']}",
            "text": t["text"],
            "muted": t["muted"],
            "title": f"bold {t['text']}",
            "bold": "bold",
            "code": t["accent"],
            "border": t["border"],
            "border.focus": t["focus"],
            "accent": t["accent"],
            "attention": t["attention"],
            "error": t["error"],
            "ready": t["accent"],
            "failed": t["error"],
            "disconnected": t["attention"],
            "selected": f"bg:{t['selected']} {t['text']}",
            "button": t["text"],
            "button.focus": f"bg:{t['selected']} {t['focus']} bold",
            "button.disabled": f"{t['muted']} italic",
            "chip": f"{t['accent']} bold",
            "user": f"bold {t['accent']}",
            "assistant": f"bold {t['text']}",
            "system": t["muted"],
        }
    )


def detect_ascii(env: Mapping[str, str] | None = None, encoding: str | None = None) -> bool:
    """True when borders must be plain ASCII (DOCKET_ASCII=1 or non-UTF-8)."""
    env = os.environ if env is None else env
    if env.get("DOCKET_ASCII", "").strip().lower() in ("1", "true", "yes"):
        return True
    enc = (encoding or "utf-8").lower().replace("_", "-")
    return "utf" not in enc


@dataclass(frozen=True)
class Glyphs:
    tl: str
    tr: str
    bl: str
    br: str
    h: str
    v: str


UNICODE = Glyphs("┌", "┐", "└", "┘", "─", "│")
ASCII = Glyphs("+", "+", "+", "+", "-", "|")

_ASCII_MAP = {
    "·": "|",
    "—": "-",
    "–": "-",
    # single cell so truncated rows keep their width (and their right border)
    "…": ".",
    "▏": "|",
    "‹": "<",
    "›": ">",
    "•": "*",
    "●": "*",
    "○": "o",
    "✖": "x",
    "✔": "v",
    "▸": ">",
    "▶": ">",
    "↑": "^",
    "↓": "v",
    "←": "<",
    "→": ">",
    "│": "|",
    "─": "-",
    "█": "#",
    "░": ".",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
}


def asciify(text: str) -> str:
    out = []
    for ch in text:
        out.append(ch if ord(ch) < 128 else _ASCII_MAP.get(ch, "?"))
    return "".join(out)
