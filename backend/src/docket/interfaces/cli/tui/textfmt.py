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


def middle_ellipsis(text: str, width: int) -> str:
    """Shorten to `width` cells keeping the start and the end (the file name)."""
    if width <= 0:
        return ""
    if cw(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    keep = width - 1
    tail_w = max(keep // 2, 1)
    head_w = keep - tail_w
    head, used = "", 0
    for ch in text:
        w = get_cwidth(ch)
        if used + w > head_w:
            break
        head += ch
        used += w
    tail, used = "", 0
    for ch in reversed(text):
        w = get_cwidth(ch)
        if used + w > tail_w:
            break
        tail = ch + tail
        used += w
    return head + "…" + tail


def unique_path_labels(paths: list[str]) -> list[str]:
    """Show each path's file name; add the shortest unique parent path on clashes.

    ``policies/handbook.pdf`` and ``archive/2024/handbook.pdf`` become
    ``policies/handbook.pdf`` and ``2024/handbook.pdf``; a unique name stays bare.
    """
    parts = [p.strip("/").split("/") for p in paths]
    out: list[str] = []
    for i, comps in enumerate(parts):
        base = comps[-1]
        rivals = [c for j, c in enumerate(parts) if j != i and c[-1] == base and c != comps]
        if not rivals:
            out.append(base)
            continue
        depth = 2
        while depth < len(comps) and any(r[-depth:] == comps[-depth:] for r in rivals):
            depth += 1
        out.append("/".join(comps[-depth:]))
    return out


# -- numbers, result glyphs -----------------------------------------------

_NUMERIC = re.compile(r"^[+\-\u2212]?[$\u00a3\u20ac]?\d[\d,]*(\.\d+)?%?$")


def is_numeric(cell: str) -> bool:
    return bool(_NUMERIC.match(cell.strip()))


def numeric_columns(rows: list[list[str]]) -> list[bool]:
    """True per column when every non-empty body cell is a number (row 0 is the header)."""
    ncols = max((len(r) for r in rows), default=0)
    flags = []
    for i in range(ncols):
        body = [r[i].strip() for r in rows[1:] if i < len(r) and r[i].strip()]
        flags.append(bool(body) and all(is_numeric(c) for c in body))
    return flags


# word -> (unicode glyph, ascii glyph, style)
RESULT_WORDS = {
    "met": ("\u2713", "OK", "class:ready"),
    "passed": ("\u2713", "OK", "class:ready"),
    "pass": ("\u2713", "OK", "class:ready"),
    "missed": ("\u2717", "X", "class:attention"),
    "failed": ("\u2717", "X", "class:attention"),
    "fail": ("\u2717", "X", "class:attention"),
}


def with_result_glyph(cell: str, ascii_mode: bool = False) -> tuple[str, str]:
    """('✓ Met', style) for result words, keeping the word; otherwise (cell, '')."""
    hit = RESULT_WORDS.get(cell.strip().lower())
    if not hit:
        return cell, ""
    glyph = hit[1] if ascii_mode else hit[0]
    return f"{glyph} {cell.strip()}", hit[2]


def pad_cell(text: str, width: int, right: bool) -> str:
    gap = max(width - cw(text), 0)
    return " " * gap + text if right else text + " " * gap


def render_grid(
    rows: list[list[str]],
    width: int,
    *,
    first_row: int = 1,
    first_col: str = "A",
    cited: frozenset[tuple[int, int]] = frozenset(),
    ascii_mode: bool = False,
) -> list[Row]:
    """A spreadsheet-style grid: column letters, row numbers, header rule, right-aligned numbers.

    `cited` holds (row index, column index) pairs (0-based, within `rows`); those
    cells are drawn as ``[value]`` and highlighted so they survive without colour.
    """
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    right = numeric_columns(rows)
    cited_cols = {c for _r, c in cited}
    shown: list[list[str]] = []
    for ri, r in enumerate(rows):
        line = []
        for ci, cell in enumerate(r):
            text, _st = with_result_glyph(cell, ascii_mode) if ri else (cell, "")
            if ci in cited_cols:
                text = f"[{text}]" if (ri, ci) in cited else f" {text} "
            line.append(text)
        shown.append(line)
    widths = [max(cw(r[i]) for r in shown) for i in range(ncols)]
    letters = [chr(ord(first_col.upper()) + i) for i in range(ncols)]
    for i in range(ncols):
        widths[i] = max(widths[i], 1)
    num_w = max(len(str(first_row + len(rows) - 1)), 1)
    gutter = " " * (num_w + 2)
    sep = " \u2502 "
    out: list[Row] = []
    ruler: Row = [("class:muted", gutter)]
    for i, letter in enumerate(letters):
        ruler.append(("class:muted", pad_cell(letter, widths[i], False)))
        if i < ncols - 1:
            ruler.append(("class:muted", " " * cw(sep)))
    out.append(ruler)
    for ri, r in enumerate(shown):
        num = str(first_row + ri).rjust(num_w)
        row: Row = [("class:muted", f"{num}  ")]
        for ci, cell in enumerate(r):
            base = "class:title" if ri == 0 else "class:text"
            if ri and (ri, ci) in cited:
                style = "class:selected class:bold"
            elif ri:
                _t, st = with_result_glyph(rows[ri][ci], ascii_mode)
                style = st or base
            else:
                style = base
            row.append((style, pad_cell(cell, widths[ci], right[ci])))
            if ci < ncols - 1:
                row.append(("class:muted", sep))
        out.append(row)
        if ri == 0:
            rule: Row = [("class:muted", gutter)]
            for ci in range(ncols):
                rule.append(("class:muted", "\u2500" * widths[ci]))
                if ci < ncols - 1:
                    rule.append(("class:muted", "\u2500\u253c\u2500"))
            out.append(rule)
    return out


def grid_width(rows: list[list[str]], first_row: int = 1) -> int:
    ncols = max((len(r) for r in rows), default=0)
    widths = [max((cw(r[i]) for r in rows if i < len(r)), default=1) + 2 for i in range(ncols)]
    return len(str(first_row + len(rows) - 1)) + 2 + sum(widths) + 3 * (ncols - 1)


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


def _render_table(lines: list[str], width: int, ascii_mode: bool = False) -> list[Row]:
    rows = [_table_cells(ln) for ln in lines]
    rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    right = numeric_columns(rows)
    shown = [rows[0]] + [[with_result_glyph(c, ascii_mode)[0] for c in r] for r in rows[1:]]
    widths = [max(cw(r[i]) for r in shown) for i in range(ncols)]
    total = sum(widths) + 3 * (ncols - 1)
    out: list[Row] = []
    if total <= width:
        for idx, r in enumerate(shown):
            row: Row = []
            for i, cell in enumerate(r):
                if idx == 0:
                    style = "class:title"
                else:
                    style = with_result_glyph(rows[idx][i], ascii_mode)[1] or "class:text"
                row.append((style, pad_cell(cell, widths[i], right[i])))
                if i < ncols - 1:
                    row.append(("class:muted", " \u2502 "))
            out.append(row)
            if idx == 0:
                rule: Row = []
                for i in range(ncols):
                    rule.append(("class:muted", "\u2500" * widths[i]))
                    if i < ncols - 1:
                        rule.append(("class:muted", "\u2500\u253c\u2500"))
                out.append(rule)
        return out
    # Narrow fallback: one record per row, never clip a value.
    header, body = rows[0], shown[1:]
    for r in body:
        for i, cell in enumerate(r):
            label = header[i] if i < len(header) else ""
            out.extend(wrap_tokens([("class:muted", label + ":"), ("", " ")] + inline_tokens(cell), width, "  "))
        out.append([])
    if out and not out[-1]:
        out.pop()
    return out


def render_markdown(text: str, width: int, base: str = "class:text", ascii_mode: bool = False) -> list[Row]:
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
            out.extend(_render_table(block, width, ascii_mode))
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
