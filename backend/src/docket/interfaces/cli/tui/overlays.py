"""Modal overlays for Screen A.

Every overlay renders itself into a list of fixed-width rows (a bordered box),
handles a small set of abstract keys, and talks to the application only through
the ``ui`` object (see ``app.DemoUI``). Overlays never touch a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui.model import Scope
from docket.interfaces.cli.tui.textfmt import (
    Row,
    cw,
    fit_row,
    inline_tokens,
    plain,
    render_markdown,
    row_width,
    truncate,
    wrap_text,
    wrap_tokens,
)
from docket.interfaces.cli.tui.theme import THEME_LABELS, THEMES, Glyphs

SEL = "class:selected"
MUTED = "class:muted"
TEXT = "class:text"


@dataclass
class Btn:
    key: str
    label: str
    enabled: bool = True
    why: str = ""
    hot: str = ""


@dataclass
class Entry:
    key: str
    label: str
    sub: str = ""
    tag: str = ""
    tag_style: str = MUTED
    enabled: bool = True
    hint: str = ""
    data: Any = None


def overlay_width(cols: int) -> int:
    if cols >= 100:
        return min(100, cols - 10)
    if cols >= 80:
        return cols - 2
    return cols


def status_style(status: str) -> str:
    return {fd.READY: "class:ready", fd.FAILED: "class:failed", fd.DISCONNECTED: "class:disconnected"}.get(status, MUTED)


class Overlay:
    name = "overlay"
    title = "Overlay"
    filterable = False  # typed characters go to the filter / input field
    has_list = True
    placeholder = "type to filter"

    def __init__(self, ui: Any) -> None:
        self.ui = ui
        self.sel = 0
        self.scroll = 0
        self.filter_text = ""
        self.msg = ""
        self.msg_style = "class:attention"
        self.stop = 0
        self.stop = self._initial_stop()

    # ---- to override -----------------------------------------------------
    def entries(self) -> list[Entry]:
        return []

    def buttons(self) -> list[Btn]:
        return [Btn("close", "Close")]

    def hint(self) -> str:
        return "Esc close"

    def preface(self, iw: int) -> list[Row]:
        return []

    def postface(self, iw: int) -> list[Row]:
        return []

    def entry_row(self, e: Entry, selected: bool, iw: int) -> Row:
        mark = "▸ " if selected else "  "
        style = TEXT if e.enabled else MUTED
        row: Row = [(style, mark + e.label)]
        tail = ""
        if e.sub:
            tail = e.sub
        left_w = row_width(row)
        tag_w = cw(e.tag) + 2 if e.tag else 0
        room = iw - left_w - tag_w - 2
        if tail and room > 3:
            row.append((MUTED, "  " + truncate(tail, room)))
        used = row_width(row)
        if e.tag:
            row.append(("", " " * max(iw - used - cw(e.tag), 1)))
            row.append((e.tag_style, e.tag))
        row = fit_row(row, iw)
        if selected:
            row = [(SEL + " " + s if s else SEL, t) for s, t in row]
        return row

    def activate(self, entry: Entry) -> None:
        pass

    def press(self, key: str) -> None:
        if key == "close":
            self.ui.close_top()

    def on_enter_input(self) -> None:
        es = self.entries()
        if es:
            self.activate(es[min(self.sel, len(es) - 1)])

    def can_close(self) -> bool:
        return True

    def left_right(self, delta: int) -> None:
        """Left/Right while a list row is selected (settings fields)."""

    # ---- structure -------------------------------------------------------
    def stops(self) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        if self.has_list:
            out.append(("list", 0))
        out.extend(("buttons", i) for i in range(len(self.buttons())))
        return out

    def _initial_stop(self) -> int:
        return 0

    @property
    def zone(self) -> str:
        s = self.stops()
        return s[min(self.stop, len(s) - 1)][0] if s else "list"

    @property
    def btn_index(self) -> int:
        s = self.stops()
        return s[min(self.stop, len(s) - 1)][1] if s else 0

    # ---- keys ------------------------------------------------------------
    def key(self, k: str) -> None:
        self.msg = ""
        handler = getattr(self, "k_" + k.replace("-", "_"), None)
        if handler:
            handler()

    def _clamp_sel(self) -> None:
        n = len(self.entries())
        self.sel = 0 if n == 0 else max(0, min(self.sel, n - 1))

    def k_up(self) -> None:
        if self.zone == "list" and self.has_list:
            self.sel -= 1
            self._clamp_sel()
        else:
            self.scroll = max(0, self.scroll - 1)

    def k_down(self) -> None:
        if self.zone == "list" and self.has_list:
            self.sel += 1
            self._clamp_sel()
        else:
            self.scroll += 1

    def k_pageup(self) -> None:
        self.scroll = max(0, self.scroll - 8)

    def k_pagedown(self) -> None:
        self.scroll += 8

    def k_home(self) -> None:
        if self.zone == "list":
            self.sel = 0
        self.scroll = 0

    def k_end(self) -> None:
        if self.zone == "list":
            self.sel = max(len(self.entries()) - 1, 0)

    def k_tab(self) -> None:
        n = len(self.stops())
        if n:
            self.stop = (self.stop + 1) % n

    def k_s_tab(self) -> None:
        n = len(self.stops())
        if n:
            self.stop = (self.stop - 1) % n

    def k_left(self) -> None:
        if self.zone == "buttons":
            first = next((i for i, s in enumerate(self.stops()) if s[0] == "buttons"), 0)
            self.stop = max(first, self.stop - 1)
        elif self.zone == "list":
            self.left_right(-1)

    def k_right(self) -> None:
        if self.zone == "buttons":
            self.stop = min(len(self.stops()) - 1, self.stop + 1)
        elif self.zone == "list":
            self.left_right(1)

    def k_enter(self) -> None:
        if self.zone == "buttons":
            btns = self.buttons()
            if not btns:
                return
            b = btns[min(self.btn_index, len(btns) - 1)]
            if not b.enabled:
                self.msg = b.why or "Unavailable."
                return
            self.press(b.key)
        else:
            self.on_enter_input()

    def k_backspace(self) -> None:
        if self.filterable and self.filter_text:
            self.filter_text = self.filter_text[:-1]
            self.sel = 0

    def k_c_u(self) -> None:
        if self.filterable:
            self.filter_text = ""
            self.sel = 0

    def text(self, s: str) -> None:
        self.msg = ""
        if self.filterable:
            self.filter_text += s.replace("\n", " ")
            self.sel = 0
            return
        if len(s) == 1:
            for b in self.buttons():
                if b.hot and b.hot == s.lower():
                    if b.enabled:
                        self.press(b.key)
                    else:
                        self.msg = b.why or "Unavailable."
                    return

    # ---- rendering -------------------------------------------------------
    def build_body(self, iw: int) -> tuple[list[Row], int | None]:
        rows = list(self.preface(iw))
        sel_row = None
        es = self.entries()
        self._clamp_sel()
        for i, e in enumerate(es):
            if i == self.sel:
                sel_row = len(rows)
            rows.append(self.entry_row(e, i == self.sel and self.zone == "list", iw))
        if not es and self.has_list and self.empty_text():
            rows.append([(MUTED, self.empty_text())])
        rows.extend(self.postface(iw))
        return rows, sel_row

    def empty_text(self) -> str:
        return "Nothing matches."

    def filter_row(self, iw: int) -> Row:
        shown = self.filter_text if self.filter_text else self.placeholder
        style = TEXT if self.filter_text else MUTED
        cursor = "▏" if self.zone == "list" else ""
        return [(MUTED, "Find: "), (style, shown), ("class:accent", cursor)]

    def compose(self, w: int, hmax: int, g: Glyphs, fill: bool = False) -> list[Row]:
        pad = 2 if w >= 80 else 1
        iw = max(w - 2 - 2 * pad, 10)
        body, sel_row = self.build_body(iw)
        if self.filterable:
            body = [self.filter_row(iw), []] + body
            sel_row = None if sel_row is None else sel_row + 2
        # chrome: buttons (wrapped), blank, hint, msg
        btn_rows = self._button_rows(iw)
        hint_rows = wrap_text(self.hint(), iw, MUTED)
        msg_rows = wrap_text(self.msg, iw, self.msg_style) if self.msg else []
        chrome = 2 + 1 + len(btn_rows) + len(hint_rows) + len(msg_rows)
        avail = max(hmax - chrome, 1)
        more = ""
        if len(body) > avail:
            avail = max(avail - 1, 1)  # one row for the "n above / n below" line
            self.scroll = max(0, min(self.scroll, len(body) - avail))
            if sel_row is not None and self.zone == "list":
                if sel_row < self.scroll:
                    self.scroll = sel_row
                elif sel_row >= self.scroll + avail:
                    self.scroll = sel_row - avail + 1
            shown = body[self.scroll : self.scroll + avail]
            above, below = self.scroll, len(body) - self.scroll - avail
            more = f"{above} above · {below} below · PgUp/PgDn to scroll"
        else:
            self.scroll = 0
            shown = body
        out: list[Row] = []
        title = f" {self.title} "
        top_fill = max(w - 2 - 1 - cw(title), 0)
        out.append(
            [("class:border.focus", g.tl + g.h), ("class:title", title), ("class:border.focus", g.h * top_fill + g.tr)]
        )

        def line(content: Row) -> Row:
            inner = fit_row(content, iw)
            return [
                ("class:border", g.v),
                ("", " " * pad),
                *inner,
                ("", " " * pad),
                ("class:border", g.v),
            ]

        for r in shown:
            out.append(line(r))
        if more:
            out.append(line([(MUTED, more)]))
        if fill:
            used = 1 + len(shown) + (1 if more else 0) + 1 + len(btn_rows) + len(msg_rows) + len(hint_rows) + 1
            for _ in range(max(hmax - used, 0)):
                out.append(line([]))
        out.append(line([]))
        for r in btn_rows:
            out.append(line(r))
        for r in msg_rows:
            out.append(line(r))
        for r in hint_rows:
            out.append(line(r))
        out.append([("class:border", g.bl + g.h * (w - 2) + g.br)])
        return out

    def _button_rows(self, iw: int) -> list[Row]:
        toks = []
        btns = self.buttons()
        for i, b in enumerate(btns):
            focused = self.zone == "buttons" and self.btn_index == i
            if not b.enabled:
                style = "class:button.disabled"
            elif focused:
                style = "class:button.focus"
            else:
                style = "class:button"
            mark = ">" if focused else " "
            label = f"{mark}[ {b.label} ]"
            if not b.enabled:
                label = f"{mark}( {b.label} )"
            toks.append((style, label))
            toks.append(("", " "))
        rows: list[Row] = [[]]
        used = 0
        for style, label in toks:
            if label == " ":
                if used:
                    rows[-1].append(("", " "))
                    used += 1
                continue
            if used + cw(label) > iw and rows[-1]:
                rows.append([])
                used = 0
            rows[-1].append((style, label))
            used += cw(label)
        return [r for r in rows if r] or [[]]


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------


class PaletteOverlay(Overlay):
    name = "palette"
    title = "Commands"
    filterable = True
    placeholder = "type a command or description"

    def _query(self) -> tuple[str, str]:
        text = self.filter_text.strip().lstrip("/")
        token, _, arg = text.partition(" ")
        return token.lower(), arg.strip()

    def entries(self) -> list[Entry]:
        q, _arg = self._query()
        out: list[tuple[int, Entry]] = []
        for e in self.ui.palette_entries():
            aliases = (e.data or "").lower().split()
            if not q:
                rank = 4
            elif q == e.key or q in aliases:
                rank = 0
            elif e.key.startswith(q):
                rank = 1
            elif any(a.startswith(q) for a in aliases):
                rank = 2
            elif q in f"{e.key} {e.sub}".lower():
                rank = 3
            else:
                continue
            out.append((rank, e))
        out.sort(key=lambda t: t[0])
        return [e for _r, e in out]

    def empty_text(self) -> str:
        return "No command matches. Backspace to widen the search."

    def entry_row(self, e: Entry, selected: bool, iw: int) -> Row:
        if not e.enabled:
            e = Entry(e.key, e.label, e.sub, "unavailable", "class:attention", False, e.hint, e.data)
        return super().entry_row(e, selected, iw)

    def buttons(self) -> list[Btn]:
        return [Btn("close", "Close")]

    def hint(self) -> str:
        return "Type to filter · Up/Down select · Enter run · Esc close"

    def postface(self, iw: int) -> list[Row]:
        es = self.entries()
        if es and 0 <= self.sel < len(es) and not es[self.sel].enabled:
            return [[], [("class:attention", "Unavailable: "), (MUTED, es[self.sel].hint)]]
        return []

    def activate(self, entry: Entry) -> None:
        if not entry.enabled:
            self.msg = entry.hint or "Unavailable."
            return
        _q, arg = self._query()
        self.ui.close_top()
        self.ui.run_command(entry.key, arg)

    def on_enter_input(self) -> None:
        if not self.entries() and self.filter_text.strip():
            # Not in the palette: treat it as a typed command so the usual
            # unknown-command / "did you mean" feedback applies.
            text = self.filter_text.strip().lstrip("/")
            self.ui.close_top()
            self.ui.run_text("/" + text)
            return
        super().on_enter_input()


# ---------------------------------------------------------------------------
# Scope picker
# ---------------------------------------------------------------------------


class ScopeOverlay(Overlay):
    name = "scope"
    title = "Search scope"
    filterable = True
    placeholder = "filter sources"

    def __init__(self, ui: Any) -> None:
        self.files_mode = False
        super().__init__(ui)

    def entries(self) -> list[Entry]:
        q = self.filter_text.strip().lower()
        cur = self.ui.scope
        out: list[Entry] = []
        if not self.files_mode:
            if not q or "all" in q:
                out.append(Entry("all", "All ready sources", "everything searchable", "current" if cur.kind == "all" else "", data=Scope()))
            for s in self.ui.world.sources:
                if q and q not in s.name.lower() and q not in s.path.lower():
                    continue
                ok = s.status == fd.READY
                tag = "current" if cur.kind == "source" and cur.source_id == s.id else ""
                out.append(
                    Entry(
                        s.id,
                        s.name,
                        s.path if ok else f"{fd.STATUS_LABEL[s.status]} — not searchable",
                        tag,
                        "class:accent",
                        ok,
                        "This source is not searchable until it is ready.",
                        Scope("source", s.id),
                    )
                )
        else:
            names: dict[str, int] = {}
            for s in self.ui.world.sources:
                if s.status == fd.READY:
                    for f in s.files:
                        if f.status == fd.FILE_READY:
                            names[f.path.rsplit("/", 1)[-1]] = names.get(f.path.rsplit("/", 1)[-1], 0) + 1
            for s in self.ui.world.sources:
                if s.status != fd.READY:
                    continue
                for f in s.files:
                    if f.status != fd.FILE_READY:
                        continue
                    base = f.path.rsplit("/", 1)[-1]
                    if q and q not in f.path.lower():
                        continue
                    sub = s.path + "/" + f.path if names.get(base, 0) > 1 else s.name
                    out.append(Entry(f"{s.id}:{f.path}", f.path, sub, "", MUTED, True, "", Scope("file", s.id, f.path)))
        return out

    def empty_text(self) -> str:
        return "No file or source matches that text."

    def buttons(self) -> list[Btn]:
        if self.files_mode:
            return [Btn("sources", "Back to sources", hot="b"), Btn("close", "Close")]
        return [Btn("files", "Find a file", hot="f"), Btn("close", "Close")]

    def hint(self) -> str:
        if self.files_mode:
            return "Type to search files · Enter select · Esc close"
        return "Type to filter · Up/Down select · Enter select · Tab actions · Esc close"

    def press(self, key: str) -> None:
        if key == "files":
            self.files_mode = True
            self.filter_text, self.sel = "", 0
            self.stop = 0
        elif key == "sources":
            self.files_mode = False
            self.filter_text, self.sel = "", 0
            self.stop = 0
        else:
            super().press(key)

    def activate(self, entry: Entry) -> None:
        if not entry.enabled:
            self.msg = entry.hint
            return
        self.ui.close_top()
        self.ui.set_scope(entry.data)


# ---------------------------------------------------------------------------
# Mode picker
# ---------------------------------------------------------------------------


class ModeOverlay(Overlay):
    name = "mode"
    title = "Answering mode"

    def entries(self) -> list[Entry]:
        out = []
        for key, label, desc, ok, why in fd.MODES:
            out.append(
                Entry(
                    key,
                    label,
                    desc,
                    "current" if key == self.ui.mode else ("unavailable" if not ok else ""),
                    "class:attention" if not ok else "class:accent",
                    ok,
                    why,
                )
            )
        return out

    def postface(self, iw: int) -> list[Row]:
        es = self.entries()
        if es and not es[self.sel].enabled:
            return [[], [("class:attention", "Unavailable: "), (MUTED, es[self.sel].hint)]]
        return []

    def hint(self) -> str:
        return "Up/Down select · Enter accept · Esc close"

    def activate(self, entry: Entry) -> None:
        if not entry.enabled:
            self.msg = entry.hint
            return
        self.ui.close_top()
        self.ui.set_mode(entry.key)


# ---------------------------------------------------------------------------
# Sources panel
# ---------------------------------------------------------------------------


class SourcesOverlay(Overlay):
    name = "sources"
    title = "Sources"
    filterable = True
    placeholder = "filter sources"

    def __init__(self, ui: Any) -> None:
        self.expanded = False
        super().__init__(ui)

    def entries(self) -> list[Entry]:
        q = self.filter_text.strip().lower()
        out = []
        for s in self.ui.world.sources:
            if q and q not in s.name.lower() and q not in s.path.lower():
                continue
            if s.status == fd.READY:
                sub = f"{s.searchable} searchable" + (f" · {s.count(fd.FILE_FAILED)} failed" if s.count(fd.FILE_FAILED) else "")
            else:
                sub = ""
            out.append(Entry(s.id, s.name, sub, fd.STATUS_LABEL[s.status], status_style(s.status), True, data=s))
        return out

    def empty_text(self) -> str:
        if not self.ui.world.sources:
            return "No sources yet. Choose Add folder to register one and start indexing."
        return "No source matches that text."

    def _selected(self):
        es = self.entries()
        return es[min(self.sel, len(es) - 1)].data if es else None

    def buttons(self) -> list[Btn]:
        s = self._selected()
        none = "Select a source first."
        return [
            Btn("add", "Add folder", hot="a"),
            Btn("refresh", "Refresh", s is not None and s.status == fd.READY, none if s is None else "Only a Ready source can be refreshed.", "r"),
            Btn("retry", "Retry", s is not None and s.status == fd.FAILED, none if s is None else "Retry applies to a Failed source.", "t"),
            Btn("reconnect", "Reconnect", s is not None and s.status == fd.DISCONNECTED, none if s is None else "Reconnect applies to a Disconnected source.", "c"),
            Btn("disconnect", "Disconnect", s is not None and s.status != fd.DISCONNECTED, none if s is None else "This source is already disconnected.", "d"),
            Btn("details", "Hide details" if self.expanded else "Details", s is not None, none, "i"),
            Btn("close", "Close"),
        ]

    def hint(self) -> str:
        return "Type to filter · Tab actions · Enter on a source toggles details · Esc close"

    def detail_rows(self, s, iw: int) -> list[Row]:
        rows: list[Row] = [
            [("class:title", s.name)],
            [(MUTED, "Path  "), (TEXT, s.path)],
            [(MUTED, "State "), (status_style(s.status), fd.STATUS_LABEL[s.status])],
            [(MUTED, "Last indexed  "), (TEXT, s.last_indexed)],
            [
                (MUTED, "Files  "),
                (TEXT, f"{s.count(fd.FILE_READY)} ready · {s.count(fd.FILE_FAILED)} failed · {s.count(fd.FILE_PENDING)} pending · {s.count(fd.FILE_EMPTY)} no text"),
            ],
        ]
        if s.note:
            rows.extend(wrap_text(s.note, iw, "class:attention"))
        files = s.files if self.expanded else [f for f in s.files if f.status in (fd.FILE_FAILED, fd.FILE_EMPTY)]
        if files:
            rows.append([])
            rows.append([(MUTED, "All files" if self.expanded else "Needs a look")])
            for f in files:
                label = fd.FILE_LABEL[f.status]
                style = "class:failed" if f.status == fd.FILE_FAILED else (MUTED if f.status != fd.FILE_READY else "class:ready")
                extra = f" — {f.error}" if f.error else (f" · {f.chunks} chunks" if f.status == fd.FILE_READY else "")
                rows.extend(
                    wrap_tokens(
                        [(TEXT, f.path), (" ", " "), (style, label), (MUTED, extra)],
                        iw,
                        "  ",
                    )
                )
        return rows

    def build_body(self, iw: int) -> tuple[list[Row], int | None]:
        es = self.entries()
        self._clamp_sel()
        s = self._selected()
        lw = 34 if iw >= 74 else iw
        list_rows: list[Row] = []
        sel_row = None
        for i, e in enumerate(es):
            selected = i == self.sel and self.zone == "list"
            if i == self.sel:
                sel_row = len(list_rows)
            head = Entry(e.key, e.label, "", e.tag, e.tag_style, e.enabled, e.hint, e.data)
            list_rows.append(self.entry_row(head, selected, lw))
            if e.sub:
                sub = fit_row([(MUTED, "    " + truncate(e.sub, lw - 4))], lw)
                if selected:
                    sub = [(SEL + " " + st if st else SEL, t) for st, t in sub]
                list_rows.append(sub)
        if not es:
            list_rows = [[(MUTED, self.empty_text())]]
        detail = self.detail_rows(s, iw if iw < 74 else iw - 37) if s else []
        if iw >= 74:
            rw = iw - 37
            rows: list[Row] = []
            n = max(len(list_rows), len(detail))
            for i in range(n):
                left = fit_row(list_rows[i], lw) if i < len(list_rows) else [("", " " * lw)]
                right = detail[i] if i < len(detail) else []
                rows.append(left + [("class:border", " │ ")] + fit_row(right, rw))
            return rows, sel_row
        rows = list_rows + ([[]] + detail if detail else [])
        return rows, sel_row

    def press(self, key: str) -> None:
        s = self._selected()
        if key == "add":
            self.ui.push(AddFolderOverlay(self.ui))
        elif key == "details":
            self.expanded = not self.expanded
        elif key == "refresh" and s:
            self.ui.start_job(s, "Refreshing", "")
        elif key == "retry" and s:
            self.ui.start_job(s, "Retrying", "ready")
        elif key == "reconnect" and s:
            self.ui.start_job(s, "Reconnecting", "ready")
        elif key == "disconnect" and s:
            self.ui.push(
                ConfirmOverlay(
                    self.ui,
                    f"Disconnect {s.name}?",
                    f"{s.name} will stop being searched. Your original files and Docket's stored copies are kept, and you can reconnect later. (Demo: nothing is changed on disk.)",
                    [("Disconnect", lambda: self.ui.disconnect_source(s.id)), ("Keep connected", None)],
                    default=1,
                )
            )
        else:
            super().press(key)

    def activate(self, entry: Entry) -> None:
        self.expanded = not self.expanded


# ---------------------------------------------------------------------------
# Add folder
# ---------------------------------------------------------------------------


class AddFolderOverlay(Overlay):
    name = "add"
    title = "Add folder"
    filterable = True
    placeholder = "/path/to/folder"

    def __init__(self, ui: Any) -> None:
        self.broad_ok = False
        super().__init__(ui)

    def entries(self) -> list[Entry]:
        t = self._path()
        if not t:
            return []
        return [Entry(p, p, "", "", MUTED, True, "", p) for p in fd.FOLDER_SUGGESTIONS if p.lower().startswith(t.lower()) and p != t]

    def _path(self) -> str:
        t = self.filter_text.strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
            t = t[1:-1]
        return t.replace("\\ ", " ")

    def filter_row(self, iw: int) -> Row:
        shown = self.filter_text or self.placeholder
        return [(MUTED, "Folder: "), (TEXT if self.filter_text else MUTED, shown), ("class:accent", "▏")]

    def empty_text(self) -> str:
        return ""

    def preface(self, iw: int) -> list[Row]:
        rows = wrap_text(
            f"Docket reads {fd.SUPPORTED_FORMATS} in this folder and its subfolders, then starts indexing right away. Original files are never changed.",
            iw,
            MUTED,
        )
        rows.append([])
        return rows

    def build_body(self, iw: int):
        rows, sel = super().build_body(iw)
        if self.entries():
            # label the suggestion rows (they follow the explanatory note)
            at = len(self.preface(iw))
            rows.insert(at, [(MUTED, "Suggestions (Enter fills the path):")])
            sel = None if sel is None else sel + 1
        return rows, sel

    def buttons(self) -> list[Btn]:
        return [Btn("add", "Add and index"), Btn("cancel", "Cancel")]

    def hint(self) -> str:
        return "Paste or type a path (quotes and spaces are fine) · Enter add · Esc cancel"

    def on_enter_input(self) -> None:
        es = self.entries()
        if es and 0 <= self.sel < len(es):
            self.filter_text = es[self.sel].data
            self.sel = 0
            return
        self._submit()

    def press(self, key: str) -> None:
        if key == "add":
            self._submit()
        else:
            self.ui.close_top()

    def _submit(self) -> None:
        path = self._path()
        if not path:
            self.msg = "Enter a folder path first."
            return
        if path in (".", "~", "/") and not self.broad_ok:
            self.broad_ok = True
            self.msg = "That is a very broad location. Press Enter again to add it anyway."
            return
        for s in self.ui.world.sources:
            if s.path == path:
                if s.status == fd.DISCONNECTED:
                    self.msg = f"{s.name} is disconnected. Use Reconnect in Sources instead of adding it again."
                else:
                    self.msg = f"{s.name} is already registered at that path."
                return
        self.ui.close_top()
        self.ui.add_folder(path)


# ---------------------------------------------------------------------------
# Indexing progress
# ---------------------------------------------------------------------------


class IndexingOverlay(Overlay):
    name = "indexing"
    has_list = False

    @property
    def title(self) -> str:  # type: ignore[override]
        j = self.ui.job
        return f"{j.verb} {j.source_name}" if j else "Indexing"

    def buttons(self) -> list[Btn]:
        j = self.ui.job
        if j is None:
            return [Btn("close", "Close")]
        if j.state == "running":
            return [Btn("hide", "Hide progress", hot="h"), Btn("stop", "Stop indexing", hot="s")]
        if j.state == "stopping":
            return [Btn("hide", "Hide progress", hot="h"), Btn("stop", "Stop indexing", False, "Stop was already requested.")]
        out = [Btn("ask", "Start asking", j.state in ("done", "stopped"), "Nothing is searchable yet.", "a")]
        if j.failed or j.state in ("failed", "stopped"):
            out.append(Btn("retry", "Retry indexing", hot="r"))
        out.append(Btn("close", "Close"))
        return out

    def hint(self) -> str:
        j = self.ui.job
        extra = " · N advance (reduced motion)" if self.ui.reduced_motion and j and j.active else ""
        return "Hide returns to chat; work continues" + extra + " · Esc hides" if j and j.active else "Esc close"

    def build_body(self, iw: int):
        j = self.ui.job
        if j is None:
            return [[(MUTED, "No indexing job is running.")]], None
        rows: list[Row] = []
        bar_w = max(min(iw - 14, 40), 10)
        frac = j.file_index / j.total if j.total else 0
        filled = int(bar_w * frac)
        bar = [("class:accent", "█" * filled), (MUTED, "░" * (bar_w - filled)), (TEXT, f" {j.file_index}/{j.total} files")]
        if j.active:
            rows.append([(TEXT, f"File {min(j.file_index + 1, j.total)} of {j.total}: "), ("class:title", j.current_file)])
            rows.append([(MUTED, "Stage: "), (TEXT, j.stage)])
            rows.append([(MUTED, "Elapsed: "), (TEXT, j.elapsed_label)])
            rows.append([])
            rows.append(bar)
            rows.append([(MUTED, "Files vary in processing time; this is a file count, not a time estimate.")])
            rows.append([])
            rows.append([(TEXT, f"{j.indexed} indexed · {j.unchanged} unchanged · {j.failed} failed")])
            if j.state == "stopping":
                rows.append([])
                rows.append([("class:attention", "Stopping after the current operation...")])
                rows.append([(MUTED, "Completed files are kept. Unfinished files stay unavailable and can be retried.")])
            return rows, None
        if j.state == "failed":
            rows.append([("class:failed", "Failed: "), (TEXT, j.reason or "Indexing could not start.")])
            rows.append([(MUTED, f"{j.source_name} stays registered. Repair the problem, then Retry indexing.")])
            return rows, None
        headline = "Indexing finished" if j.state == "done" else "Stopped before finishing"
        style = "class:ready" if j.state == "done" and not j.failed else "class:attention"
        label = "Completed" if j.state == "done" and not j.failed else ("Partial" if j.state == "done" else "Cancelled")
        rows.append([(style, f"{label}: "), (TEXT, headline)])
        rows.append([(TEXT, f"{j.indexed} indexed · {j.unchanged} unchanged · {j.failed} failed")])
        rows.append([(MUTED, f"{j.file_index} of {j.total} files processed in {j.elapsed_label}")])
        if j.state == "stopped":
            rows.append([(MUTED, "Completed files were kept; the rest are unavailable until you retry.")])
        if j.failures:
            rows.append([])
            rows.append([("class:title", "Failures")])
            for name, reason in j.failures:
                rows.extend(wrap_tokens([(TEXT, name), (" ", " "), ("class:failed", "Failed"), (MUTED, f" — {reason}")], iw, "  "))
            rows.append([(MUTED, "Recovery: fix or replace the file, then Retry indexing.")])
        return rows, None

    def press(self, key: str) -> None:
        j = self.ui.job
        if key == "hide":
            self.ui.close_top()
        elif key == "stop":
            self.ui.stop_job()
        elif key == "retry" and j:
            src = self.ui.world.find(j.source_id)
            self.ui.close_top()
            if src:
                self.ui.start_job(src, "Retrying", j.on_done)
        elif key == "ask":
            self.ui.close_top()
            self.ui.notice("Ready. Ask a question below.")
        else:
            self.ui.close_top()

    def text(self, s: str) -> None:
        if s.lower() == "n" and self.ui.reduced_motion:
            self.ui.tick()
            return
        super().text(s)


# ---------------------------------------------------------------------------
# Evidence and answer details
# ---------------------------------------------------------------------------


class EvidenceOverlay(Overlay):
    name = "evidence"
    has_list = False

    def __init__(self, ui: Any, answer_pos: int, cit: int = 0) -> None:
        self.answer_pos = answer_pos
        self.cit = cit
        self.show_details = False
        super().__init__(ui)

    def _answer(self):
        answers = self.ui.answers()
        return answers[self.answer_pos].answer if 0 <= self.answer_pos < len(answers) else None

    @property
    def title(self) -> str:  # type: ignore[override]
        a = self._answer()
        n = len(a.citations) if a else 0
        return f"Evidence [{self.cit + 1}] of {n}"

    def _cit(self):
        a = self._answer()
        if a is None or not a.citations:
            return None
        return a.citations[max(0, min(self.cit, len(a.citations) - 1))]

    def buttons(self) -> list[Btn]:
        c = self._cit()
        a = self._answer()
        n = len(a.citations) if a else 0
        return [
            Btn("prev", "Previous", self.cit > 0, "This is the first citation.", "p"),
            Btn("next", "Next", self.cit < n - 1, "This is the last citation.", "n"),
            Btn("open", "Open original", bool(c and c.available), "Unavailable: this citation's stored version cannot be resolved.", "o"),
            Btn("details", "Hide details" if self.show_details else "Details", c is not None, "", "d"),
            Btn("close", "Close"),
        ]

    def hint(self) -> str:
        return "P previous · N next · O open original · D details · PgUp/PgDn scroll · Esc close"

    def build_body(self, iw: int):
        c = self._cit()
        if c is None:
            return [[(MUTED, "This answer has no citations.")]], None
        rows: list[Row] = [[("class:title", c.source_name)]]
        if not c.available:
            rows.append([("class:attention", "Unavailable: "), (TEXT, "this evidence can no longer be shown.")])
            rows.extend(wrap_text(c.reason, iw, MUTED))
            rows.append([])
            rows.extend(wrap_text("The answer text is unchanged; only the passage behind this citation is missing.", iw, MUTED))
        else:
            loc = c.location or "Location not extracted for this passage"
            rows.append([(MUTED, "Location  "), (TEXT if c.location else "class:attention", loc)])
            rows.append([(MUTED, "Indexed   "), (TEXT, f"{c.indexed} · {c.version}")])
            rows.append([])
            rows.append([(MUTED, "Passage (verbatim)")])
            for para in c.passage.split("\n"):
                if not para:
                    rows.append([("class:border", "│")])
                    continue
                for r in wrap_text(para, iw - 2, TEXT):
                    rows.append([("class:border", "│ ")] + r)
        if self.show_details:
            rows.append([])
            rows.append([("class:title", "Details")])
            rows.append([(MUTED, "File     "), (TEXT, c.rel_path)])
            rows.append([(MUTED, "Chunk    "), (TEXT, c.chunk_id)])
            rows.append([(MUTED, "Version  "), (TEXT, c.version)])
            rows.append([(MUTED, "Confidence not calibrated; no percentage is shown.")])
        return rows, None

    def press(self, key: str) -> None:
        if key == "prev":
            self.cit = max(0, self.cit - 1)
            self.scroll = 0
        elif key == "next":
            a = self._answer()
            self.cit = min(self.cit + 1, (len(a.citations) if a else 1) - 1)
            self.scroll = 0
        elif key == "open":
            c = self._cit()
            self.msg_style = "class:accent"
            self.msg = f"Demo: Docket would confirm {c.rel_path if c else 'the file'} still matches the stored version, then open it with your system opener."
        elif key == "details":
            self.show_details = not self.show_details
        else:
            super().press(key)


class DetailsOverlay(Overlay):
    name = "details"
    title = "Answer details"
    has_list = False

    def __init__(self, ui: Any, answer_pos: int) -> None:
        self.answer_pos = answer_pos
        super().__init__(ui)

    def buttons(self) -> list[Btn]:
        a = self._a()
        return [Btn("evidence", "Evidence", bool(a and a.citations), "This answer has no citations.", "e"), Btn("close", "Close")]

    def _a(self):
        answers = self.ui.answers()
        return answers[self.answer_pos].answer if 0 <= self.answer_pos < len(answers) else None

    def build_body(self, iw: int):
        a = self._a()
        if a is None:
            return [[(MUTED, "There is no answer yet.")]], None
        rows: list[Row] = []

        def kv(k: str, v: str, style: str = TEXT) -> None:
            rows.extend(wrap_tokens([(MUTED, f"{k:<14}"), (style, v)], iw, " " * 14, ""))

        kv("Mode", a.mode_label)
        kv("Elapsed", f"{a.elapsed:.1f}s")
        kv("Scope", a.scope_label)
        kv("Follow-up", a.rewrite or "Not rewritten")
        kv("Period", a.ambiguity or "No ambiguity noticed")
        kv("Computation", a.compute)
        kv("Citations", f"{len(a.citations)} (syntax valid; meaning not machine-verified)")
        kv("Confidence", "Not calibrated")
        if a.abstained:
            kv("Why no answer", a.abstain_reason, "class:attention")
        for w in a.warnings:
            kv("Warning", w, "class:attention")
        return rows, None

    def press(self, key: str) -> None:
        if key == "evidence":
            self.ui.close_top()
            self.ui.open_evidence(self.answer_pos, 0)
        else:
            super().press(key)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

SETTINGS_TABS = ("answering", "appearance", "history", "system")
TAB_LABELS = {"answering": "Answering", "appearance": "Appearance", "history": "History", "system": "System"}
THINK = fd.THINKING_LEVELS


class SettingsOverlay(Overlay):
    name = "settings"
    title = "Settings"

    def __init__(self, ui: Any, tab: str = "answering") -> None:
        self.tab = tab
        self.pending: dict[str, Any] = dict(ui.settings_values())
        super().__init__(ui)

    # stops: tabs, list, buttons
    def stops(self) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = [("tabs", 0)]
        if self.entries():
            out.append(("list", 0))
        out.extend(("buttons", i) for i in range(len(self.buttons())))
        return out

    @property
    def dirty(self) -> bool:
        return self.pending != self.ui.settings_values()

    def can_close(self) -> bool:
        return not self.dirty

    def entries(self) -> list[Entry]:
        p = self.pending
        e: list[Entry] = []
        if self.tab == "answering":
            model_note = next((n for m, ok, n in fd.ANSWER_MODELS if m == p["model"]), "")
            e.append(Entry("model", "Answer model", f"‹ {p['model']} ›", "", MUTED, True, model_note))
            mode_label = next(lbl for k, lbl, *_ in fd.MODES if k == p["mode"])
            e.append(Entry("mode", "Answering mode", f"‹ {mode_label} ›"))
            e.append(Entry("thinking", "Answer thinking", f"‹ {THINK[p['thinking']]} ›", hint="Slower and more careful as you move right."))
            e.append(Entry("embed", "Embedding model", f"{fd.EMBED_MODEL} (read-only)", enabled=False, hint="Read-only here. Changing it needs a rebuild of the search index, offered under Maintenance."))
        elif self.tab == "appearance":
            e.append(Entry("theme", "Appearance", f"‹ {THEME_LABELS[p['theme']]} ›"))
            e.append(Entry("density", "Density", "Comfortable (read-only)", enabled=False, hint="Density and animation preferences arrive in a later release."))
        elif self.tab == "history":
            e.append(Entry("history", "Input history", f"‹ {'On' if p['history'] else 'Off'} ›", hint="Stores what you type, not answers. DOCKET_NO_HISTORY turns it off for the whole process."))
        else:
            for k, v in (
                ("Data folder", fd.DATA_DIR_LABEL),
                ("Ollama", "Available (demo)"),
                ("Answer model", p["model"]),
                ("Embedding", fd.EMBED_MODEL),
                ("Index", "Compatible with the embedding model (demo)"),
                ("Coverage", f"{self.ui.world.ready_files()} files searchable"),
            ):
                e.append(Entry(k, k, v, enabled=False, hint="Read-only information."))
        return e

    def entry_row(self, e: Entry, selected: bool, iw: int) -> Row:
        mark = "▸ " if selected else "  "
        label = f"{e.label:<18}"
        style = TEXT if e.enabled else MUTED
        row: Row = [(style, mark + label), (style if e.enabled else MUTED, e.sub)]
        row = fit_row(row, iw)
        if selected:
            row = [(SEL, t) for _s, t in row]
        return row

    def preface(self, iw: int) -> list[Row]:
        tabs: Row = []
        for t in SETTINGS_TABS:
            focused = self.zone == "tabs" and t == self.tab
            style = SEL if t == self.tab else MUTED
            if focused:
                style = "class:button.focus"
            tabs.append((style, f" {TAB_LABELS[t]} "))
            tabs.append(("", " "))
        return [tabs, []]

    def postface(self, iw: int) -> list[Row]:
        es = self.entries()
        rows: list[Row] = []
        if self.tab == "answering":
            for m, ok, note in fd.ANSWER_MODELS:
                if not ok:
                    rows.append([(MUTED, f"{m}: {note}")])
        if es and self.zone == "list" and es[min(self.sel, len(es) - 1)].hint:
            rows.append([])
            rows.extend(wrap_text(es[min(self.sel, len(es) - 1)].hint, iw, MUTED))
        if self.tab == "history":
            rows.append([])
            rows.extend(wrap_text("Conversation memory is kept in this session only; it is not saved when you exit.", iw, MUTED))
        if self.tab == "appearance":
            rows.append([])
            rows.extend(wrap_text("The terminal font and size are set by your terminal, not by Docket.", iw, MUTED))
        if self.dirty:
            rows.append([])
            rows.append([("class:attention", "Unsaved changes")])
        return rows

    def buttons(self) -> list[Btn]:
        return [
            Btn("save", "Save", self.dirty, "Nothing to save."),
            Btn("check", "Check readiness"),
            Btn("cancel", "Cancel"),
        ]

    def hint(self) -> str:
        return "Tab move · Left/Right change · Enter activate · Esc cancel"

    def _initial_stop(self) -> int:
        return 1 if self.entries() else 0

    def k_down(self) -> None:
        if self.zone == "tabs" and self.entries():
            self.stop = 1
        else:
            super().k_down()

    def k_up(self) -> None:
        if self.zone == "list" and self.sel == 0:
            self.stop = 0
        else:
            super().k_up()

    def k_left(self) -> None:
        if self.zone == "tabs":
            i = SETTINGS_TABS.index(self.tab)
            self._set_tab(SETTINGS_TABS[max(0, i - 1)])
        else:
            super().k_left()

    def k_right(self) -> None:
        if self.zone == "tabs":
            i = SETTINGS_TABS.index(self.tab)
            self._set_tab(SETTINGS_TABS[min(len(SETTINGS_TABS) - 1, i + 1)])
        else:
            super().k_right()

    def _set_tab(self, t: str) -> None:
        self.tab, self.sel = t, 0

    def on_enter_input(self) -> None:
        self.left_right(1)

    def left_right(self, delta: int) -> None:
        es = self.entries()
        if not es:
            return
        e = es[min(self.sel, len(es) - 1)]
        if not e.enabled:
            self.msg = e.hint
            return
        p = self.pending
        if e.key == "model":
            installed = [m for m, ok, _ in fd.ANSWER_MODELS if ok]
            p["model"] = installed[(installed.index(p["model"]) + delta) % len(installed)]
        elif e.key == "mode":
            avail = [k for k, _l, _d, ok, _w in fd.MODES if ok]
            p["mode"] = avail[(avail.index(p["mode"]) + delta) % len(avail)]
        elif e.key == "thinking":
            p["thinking"] = (p["thinking"] + delta) % len(THINK)
        elif e.key == "theme":
            p["theme"] = THEMES[(THEMES.index(p["theme"]) + delta) % len(THEMES)]
        elif e.key == "history":
            p["history"] = not p["history"]

    def press(self, key: str) -> None:
        if key == "save":
            self.ui.apply_settings(dict(self.pending))
            self.ui.close_top(force=True)
        elif key == "check":
            self.msg_style = "class:accent"
            self.msg = "Readiness check (demo): Ollama available, models installed, index compatible."
        else:
            self.ui.close_top()


# ---------------------------------------------------------------------------
# Welcome / readiness, confirmations, jobs
# ---------------------------------------------------------------------------


class WelcomeOverlay(Overlay):
    name = "welcome"
    title = "Welcome to Docket"
    has_list = False

    def __init__(self, ui: Any, blocked: bool = False) -> None:
        self.blocked = blocked
        super().__init__(ui)

    def buttons(self) -> list[Btn]:
        return [
            Btn("add", "Add a folder", hot="a"),
            Btn("check", "Check again", hot="c"),
            Btn("settings", "Settings", hot="s"),
            Btn("close", "Close"),
        ]

    def hint(self) -> str:
        return "Tab move · Enter activate · Esc close"

    def build_body(self, iw: int):
        rows = wrap_text("Ask questions about documents stored on this PC.", iw, TEXT)
        rows.append([])
        items = fd.READINESS_BLOCKED if self.blocked else fd.READINESS_OK
        for k, v in items:
            bad = self.blocked and ("not" in v.lower())
            rows.append([(MUTED, f"{k:<22}"), ("class:attention" if bad else TEXT, v)])
        if self.blocked:
            rows.append([])
            rows.extend(wrap_text(fd.BLOCKED_HELP, iw, "class:attention"))
            rows.extend(wrap_text("Docket never installs models or starts services for you.", iw, MUTED))
        return rows, None

    def press(self, key: str) -> None:
        if key == "add":
            self.ui.push(AddFolderOverlay(self.ui))
        elif key == "check":
            self.msg_style = "class:accent" if not self.blocked else "class:attention"
            self.msg = "Checked just now (demo): " + ("still blocked." if self.blocked else "everything is available.")
        elif key == "settings":
            self.ui.push(SettingsOverlay(self.ui, "system"))
        else:
            super().press(key)


class ConfirmOverlay(Overlay):
    name = "confirm"
    has_list = False

    def __init__(self, ui: Any, title: str, message: str, actions: list[tuple[str, Callable[[], None] | None]], default: int = 0) -> None:
        self._title = title
        self.message = message
        self.actions = actions
        super().__init__(ui)
        self.stop = default

    @property
    def title(self) -> str:  # type: ignore[override]
        return self._title

    def build_body(self, iw: int):
        return wrap_text(self.message, iw, TEXT), None

    def buttons(self) -> list[Btn]:
        return [Btn(str(i), label) for i, (label, _cb) in enumerate(self.actions)]

    def hint(self) -> str:
        return "Left/Right choose · Enter confirm · Esc go back"

    def press(self, key: str) -> None:
        cb = self.actions[int(key)][1]
        self.ui.close_top(force=True)
        if cb:
            cb()


class JobsOverlay(Overlay):
    name = "jobs"
    title = "Jobs"

    def entries(self) -> list[Entry]:
        out: list[Entry] = []
        j = self.ui.job
        if j is not None:
            state = {"running": "Running", "stopping": "Stopping", "done": "Completed", "stopped": "Cancelled", "failed": "Failed"}[j.state]
            out.append(Entry("current", j.source_name, f"{j.file_index}/{j.total} files", state, "class:accent", data="current"))
        for name, state, when, summary in fd.JOB_HISTORY:
            style = {"Completed": "class:ready", "Partial": "class:attention", "Interrupted": "class:attention"}.get(state, MUTED)
            out.append(Entry(name, name, when, state, style, data=summary))
        return out

    def buttons(self) -> list[Btn]:
        return [Btn("results", "Results", hot="r"), Btn("close", "Close")]

    def hint(self) -> str:
        return "Up/Down select · Enter results · Esc close (history is demo data)"

    def activate(self, entry: Entry) -> None:
        if entry.data == "current":
            self.ui.close_top()
            self.ui.push(IndexingOverlay(self.ui))
        else:
            self.msg_style = "class:text"
            self.msg = f"{entry.key}: {entry.data}"

    def press(self, key: str) -> None:
        if key == "results":
            es = self.entries()
            if es:
                self.activate(es[min(self.sel, len(es) - 1)])
        else:
            super().press(key)
