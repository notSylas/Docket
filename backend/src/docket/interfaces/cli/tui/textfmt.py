"""Width-aware formatted-text helpers and a small Markdown-ish renderer."""

from __future__ import annotations

import re
from typing import Iterable

from prompt_toolkit.utils import get_cwidth

Frag = tuple[str, str]
Row = list[Frag]

SEP = " · "


def cw(text: str) -> int:
    return sum(get_cwidth(c) for c in text)


def row_width(row: Row) -> int:
    return sum(cw(t) for _, t in row)


def plain(row: Row) -> str:
    return "".join(t for _, t in row)


def truncate_row(row: Row, width: int) -> Row:
    """Cut a row to at most `width` cells, ending in an ellipsis if cut."""
    if width <= 0:
        return []
    if row_width(row) <= width:
        return list(row)
    out: Row = []
    used = 0
    limit = width - 1
    for style, text in row:
        buf = ""
        for ch in text:
            w = get_cwidth(ch)
            if used + w > limit:
                if buf:
                    out.append((style, buf))
                out.append((style, "…"))
                return out
            buf += ch
            used += w
        out.append((style, buf))
    return out


def fit_row(row: Row, width: int, style: str = "") -> Row:
    """Truncate or right-pad a row to exactly `width` cells."""
    out = truncate_row(row, width)
    pad = width - row_width(out)
    if pad > 0:
        out.append((style, " " * pad))
    return out


def truncate(text: str, width: int) -> str:
    return plain(truncate_row([("", text)], width))


# -- inline markup and wrapping ------------------------------------------

_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[\d+\])")


def inline_tokens(text: str, base: str = "class:text") -> list[Frag]:
    """Split text into word / space tokens carrying styles for **bold**, `code`, [n]."""
    tokens: list[Frag] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        style = base
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, style = part[2:-2], base + " class:bold"
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, style = part[1:-1], "class:code"
        elif re.fullmatch(r"\[\d+\]", part):
            style = "class:chip"
        for piece in re.findall(r"\s+|\S+", part):
            tokens.append((style, " " if piece.isspace() else piece))
    return tokens


def wrap_tokens(tokens: list[Frag], width: int, indent: str = "", first: str | None = None) -> list[Row]:
    """Greedy word wrap. `first` is the prefix of the first line (default indent)."""
    width = max(width, 4)
    rows: list[Row] = []
    prefix = indent if first is None else first
    cur: Row = []
    used = cw(prefix)
    avail_prefix = prefix
    for style, tok in tokens:
        w = cw(tok)
        if tok == " ":
            if cur:
                cur.append((style, " "))
                used += 1
            continue
        if used + w > width and cur:
            while cur and cur[-1][1] == " ":
                cur.pop()
            rows.append([("", avail_prefix)] + cur if avail_prefix else cur)
            cur = []
            avail_prefix = indent
            used = cw(indent)
        while w > width - used and not cur and w > 0:  # hard-split very long tokens
            room = max(width - used, 1)
            head, acc = "", 0
            for ch in tok:
                if acc + get_cwidth(ch) > room:
                    break
                head += ch
                acc += get_cwidth(ch)
            if not head:
                break
            rows.append([("", avail_prefix), (style, head)] if avail_prefix else [(style, head)])
            tok = tok[len(head):]
            w = cw(tok)
            avail_prefix = indent
            used = cw(indent)
        cur.append((style, tok))
        used += w
    if cur or not rows:
        while cur and cur[-1][1] == " ":
            cur.pop()
        rows.append([("", avail_prefix)] + cur if avail_prefix else cur)
    return rows


def wrap_text(text: str, width: int, style: str = "class:text", indent: str = "") -> list[Row]:
    rows: list[Row] = []
    for line in text.split("\n"):
        rows.extend(wrap_tokens(inline_tokens(line, style), width, indent))
    return rows


# -- Markdown-ish blocks --------------------------------------------------

_BULLET = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _render_table(lines: list[str], width: int) -> list[Row]:
    rows = [_table_cells(ln) for ln in lines]
    rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(cw(r[i]) for r in rows) for i in range(ncols)]
    total = sum(widths) + 3 * (ncols - 1)
    out: list[Row] = []
    if total <= width:
        for idx, r in enumerate(rows):
            row: Row = []
            for i, cell in enumerate(r):
                style = "class:title" if idx == 0 else "class:text"
                row.append((style, cell + " " * (widths[i] - cw(cell))))
                if i < ncols - 1:
                    row.append(("class:muted", " │ "))
            out.append(row)
            if idx == 0:
                out.append([("class:muted", "─" * min(total, width))])
        return out
    # Narrow fallback: one record per row, never clip a value.
    header, body = rows[0], rows[1:]
    for r in body:
        for i, cell in enumerate(r):
            label = header[i] if i < len(header) else ""
            out.extend(wrap_tokens([("class:muted", label + ":"), ("", " ")] + inline_tokens(cell), width, "  "))
        out.append([])
    if out and not out[-1]:
        out.pop()
    return out


def render_markdown(text: str, width: int, base: str = "class:text") -> list[Row]:
    """Paragraphs, bullet/numbered lists, fenced code and tables, wrapped to width."""
    out: list[Row] = []
    lines = text.split("\n")
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.extend(wrap_tokens(inline_tokens(" ".join(s.strip() for s in para), base), width))
            para.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush_para()
            if out and out[-1]:
                out.append([])
            i += 1
        elif line.lstrip().startswith("```"):
            flush_para()
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                out.append([("class:code", "  " + lines[i])])
                i += 1
            i += 1
        elif line.lstrip().startswith("|"):
            flush_para()
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.extend(_render_table(block, width))
        elif _BULLET.match(line):
            flush_para()
            m = _BULLET.match(line)
            assert m
            marker = "•" if m.group(2) in "-*" else m.group(2)
            out.extend(
                wrap_tokens(inline_tokens(m.group(3), base), width, "  ", f"{marker} ")
            )
            i += 1
        elif line.startswith("#"):
            flush_para()
            out.extend(wrap_tokens(inline_tokens(line.lstrip("# "), "class:title"), width))
            i += 1
        else:
            para.append(line)
            i += 1
    flush_para()
    while out and not out[-1]:
        out.pop()
    return out


def indent_rows(rows: Iterable[Row], prefix: str) -> list[Row]:
    return [([("", prefix)] + r) if r else [] for r in rows]
