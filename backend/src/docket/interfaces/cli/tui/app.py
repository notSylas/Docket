"""Screen A prototype: one prompt_toolkit Application, fake data only.

Structure (spec section 6.2): views are the ``*_fragments`` methods and the
overlay classes; this class is the interaction controller (commands, overlay
stack, drafts, operation ownership); the "application/services" layer is
``fake_data`` and nothing else.
"""

from __future__ import annotations

import asyncio
import math
import sys
from dataclasses import dataclass
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor
from prompt_toolkit.styles import DynamicStyle

from docket.interfaces.cli.interactive.commands import (
    BARE_WORDS,
    CommandRegistry,
    SlashCommand,
    default_registry,
)
from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui import overlays as ov
from docket.interfaces.cli.tui.model import IndexJob, Message, QueryOp, Scope
from docket.interfaces.cli.tui.textfmt import (
    Row,
    cw,
    fit_row,
    indent_rows,
    render_markdown,
    row_width,
    truncate_row,
    wrap_text,
    wrap_tokens,
    inline_tokens,
)
from docket.interfaces.cli.tui.theme import (
    ASCII,
    UNICODE,
    asciify,
    detect_ascii,
    make_style,
)

STATES = ("welcome", "welcome-blocked", "chat", "indexing", "sources", "evidence", "settings")
MIN_COLS, MIN_ROWS = 60, 16
COMPOSER_MAX_LINES = 5


@dataclass(frozen=True)
class Metrics:
    cols: int
    rows: int
    composer_lines: int
    notice_rows: int
    transcript_h: int
    text_w: int
    breakpoint: str  # wide | medium | narrow | small


def breakpoint_for(cols: int, rows: int) -> str:
    if cols < MIN_COLS or rows < MIN_ROWS:
        return "small"
    if cols >= 100:
        return "wide"
    if cols >= 80:
        return "medium"
    return "narrow"


class _DynamicTopFloat(Float):
    """A Float whose top row is computed at draw time (stock Float takes an int)."""

    def __init__(self, top_fn: Callable[[], int], **kw: Any) -> None:
        self._top_fn = top_fn
        super().__init__(**kw)

    @property
    def top(self) -> int:  # type: ignore[override]
        return self._top_fn()

    @top.setter
    def top(self, _value: Any) -> None:
        pass


def _call(method: str) -> Callable[[Any, str], bool | None]:
    def handler(session: Any, arg: str) -> bool | None:
        return getattr(session, method)(arg)

    return handler


def build_registry() -> CommandRegistry:
    """The existing registry (same names, aliases, prefix rules) plus Screen A additions."""
    reg = default_registry()
    for name, summary, _why in fd.COMMAND_EXTRAS:
        reg.register(SlashCommand(name, summary, _call("cmd_" + name)))
    return reg


class DemoUI:
    def __init__(
        self,
        state: str = "chat",
        *,
        ascii_mode: bool | None = None,
        reduced_motion: bool = False,
        theme: str = "dark",
        input: Any = None,
        output: Any = None,
        tick_interval: float = 0.5,
    ) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; choose from {', '.join(STATES)}")
        self.state_name = state
        self.ascii_mode = (
            detect_ascii(encoding=getattr(sys.stdout, "encoding", None)) if ascii_mode is None else ascii_mode
        )
        self.reduced_motion = reduced_motion
        self.theme = theme
        self.tick_interval = tick_interval
        self.glyphs = ASCII if self.ascii_mode else UNICODE
        self.blocked = state == "welcome-blocked"

        self.world = fd.World.fresh(empty=state.startswith("welcome"))
        self.scope = Scope()
        self.mode = "auto"
        self.model = fd.DEFAULT_MODEL
        self.thinking = 1
        self.history_enabled = True
        self.messages: list[Message] = []
        self._uid = 0
        self.overlays: list[ov.Overlay] = []
        self.job: IndexJob | None = None
        self.query_op: QueryOp | None = None
        self.last_question = ""
        self.scroll = 0  # lines above the bottom of the transcript
        self.unread = False
        self.focus_answer = -1
        self.hint_text = ""
        self.registry = build_registry()
        self._style_cache: dict[str, Any] = {}
        self._line_cache: dict[tuple, list[Row]] = {}

        self._seed_state(state)
        self.composer = Buffer(multiline=True, name="composer", on_text_changed=lambda _b: self._clear_hint())
        self._build_application(input, output)
        self._sync_focus()

    # ------------------------------------------------------------------
    # seeding
    # ------------------------------------------------------------------
    def _seed_state(self, state: str) -> None:
        self.add_message(Message("system", fd.INTRO_NOTICE))
        if state in ("chat", "indexing", "sources", "evidence", "settings"):
            self.add_message(Message("user", fd.initial_question()))
            a = fd.initial_answer()
            self.add_message(Message("assistant", a.text, answer=a, mode_label=a.mode_label, elapsed=a.elapsed, scope_label=a.scope_label))
            self.last_question = fd.initial_question()
            self.focus_answer = 0
        if state == "welcome":
            self.overlays.append(ov.WelcomeOverlay(self))
        elif state == "welcome-blocked":
            self.overlays.append(ov.WelcomeOverlay(self, blocked=True))
        elif state == "indexing":
            fin = self.world.sources[0]
            self.job = IndexJob(fin.id, fin.name, "Indexing", total=fd.INDEX_TOTAL_FILES, file_index=6, stage_index=5, ticks=21, indexed=5, unchanged=1)
            self.overlays.append(ov.IndexingOverlay(self))
        elif state == "sources":
            self.overlays.append(ov.SourcesOverlay(self))
        elif state == "evidence":
            self.overlays.append(ov.EvidenceOverlay(self, 0, 0))
        elif state == "settings":
            self.overlays.append(ov.SettingsOverlay(self))

    # ------------------------------------------------------------------
    # messages / transcript
    # ------------------------------------------------------------------
    def add_message(self, msg: Message) -> None:
        self._uid += 1
        msg.uid = self._uid
        self.messages.append(msg)
        if self.scroll > 0:
            self.unread = True

    def notice(self, text: str, level: str = "info") -> None:
        self.add_message(Message("system", text, level=level))
        self.invalidate()

    def answers(self) -> list[Message]:
        return [m for m in self.messages if m.role == "assistant" and m.answer is not None]

    def _render_message(self, m: Message, width: int, focused: bool) -> list[Row]:
        key = (m.uid, width, focused)
        if key in self._line_cache:
            return self._line_cache[key]
        rows: list[Row] = []
        if m.role == "user":
            rows.append([("class:user", "You")])
            rows.extend(indent_rows(wrap_text(m.text, width - 2, "class:text"), "  "))
        elif m.role == "assistant":
            label = ("› " if focused else "") + "Docket"
            rows.append([("class:assistant", label)])
            rows.extend(indent_rows(render_markdown(m.text, width - 2), "  "))
            a = m.answer
            if a and a.citations:
                rows.append([])
                rows.append([("class:muted", "  Sources:")])
                for n, c in enumerate(a.citations, 1):
                    tail = [] if c.available else [("class:attention", "  (unavailable)")]
                    rows.append(truncate_row([("", "    "), ("class:chip", f"[{n}]"), ("class:text", f" {c.source_name}"), *tail], width))
            if a:
                for w in a.warnings:
                    rows.extend(indent_rows(wrap_text(f"Warning: {w}", width - 2, "class:attention"), "  "))
            meta = [m.mode_label or "Quick search"]
            if m.elapsed is not None:
                meta.append(f"{m.elapsed:.1f}s")
            if m.scope_label and m.scope_label != "All ready sources":
                meta.append(f"scope: {m.scope_label}")
            if a and a.abstained:
                meta.insert(0, "No answer")
            rows.append([])
            rows.extend(
                indent_rows(
                    wrap_tokens(
                        [("class:muted", " · ".join(meta) + "   "), ("class:chip", "[Evidence F5]"), (" ", " "), ("class:chip", "[Details F6]")],
                        width - 2,
                    ),
                    "  ",
                )
            )
        else:
            label, style = {"info": ("Notice", "class:system"), "attention": ("Attention", "class:attention"), "error": ("Failed", "class:error")}[m.level]
            rows.extend(wrap_tokens([(style + " class:bold", label + ": ")] + inline_tokens(m.text, style), width, "  "))
        rows.append([])
        self._line_cache[key] = rows
        return rows

    def transcript_rows(self, width: int) -> list[Row]:
        answers = self.answers()
        focus_uid = answers[self.focus_answer].uid if answers and 0 <= self.focus_answer < len(answers) and len(answers) > 1 else -1
        out: list[Row] = []
        for m in self.messages:
            out.extend(self._render_message(m, width, m.uid == focus_uid))
        if self.query_op and self.query_op.active:
            if self.query_op.state == "stopping":
                out.append([("class:attention", "Stopping after the current operation...")])
            else:
                out.append([("class:accent", f"{self.query_op.stage}..."), ("class:muted", "  Ctrl+C to cancel")])
        return out

    # ------------------------------------------------------------------
    # metrics
    # ------------------------------------------------------------------
    def size(self) -> tuple[int, int]:
        s = self.application.output.get_size()
        return s.columns, s.rows

    def readiness_notice(self) -> str:
        if self.world.ready_files() == 0 and not (self.job and self.job.active):
            if self.blocked:
                return "Not ready: the answer model is missing. Open Settings (F8) for details."
            return "Nothing searchable yet. Press F2 for Sources, then Add folder."
        return ""

    def metrics(self) -> Metrics:
        cols, rows = self.size()
        inner = max(cols - 4, 10)
        lines = 0
        for ln in (self.composer.text or "").split("\n"):
            lines += max(1, math.ceil(cw(ln) / inner))
        lines = max(1, min(lines, COMPOSER_MAX_LINES))
        notice = 1 if self.readiness_notice() and rows >= 20 else 0
        transcript_h = max(rows - 1 - notice - (lines + 2) - 1, 1)
        return Metrics(cols, rows, lines, notice, transcript_h, max(min(cols - 4, 100), 20), breakpoint_for(cols, rows))

    def too_small(self) -> bool:
        cols, rows = self.size()
        return cols < MIN_COLS or rows < MIN_ROWS

    # ------------------------------------------------------------------
    # fragments (views)
    # ------------------------------------------------------------------
    def _fix(self, frags: list[tuple[str, str]]) -> FormattedText:
        if self.ascii_mode:
            frags = [(s, asciify(t)) for s, t in frags]
        return FormattedText(frags)

    def scope_label(self) -> str:
        return self.scope.label(self.world)

    def mode_label(self) -> str:
        return next(lbl for k, lbl, *_ in fd.MODES if k == self.mode)

    def header_rows(self, cols: int) -> Row:
        scope, mode, model = self.scope_label(), self.mode_label(), self.model
        tag = fd.DEMO_TAG
        sep = " · "
        fixed = cw("DOCKET") + cw(sep) * 2 + cw(mode) + cw(tag) + 3
        show_model = cols >= 80
        if show_model:
            fixed += cw(sep) + cw(model)
        scope_room = max(cols - fixed, 8)
        scope_t = scope if cw(scope) <= scope_room else scope[: scope_room - 1] + "…"
        row: Row = [("class:accent class:bold", " DOCKET"), ("class:muted", sep), ("class:text", scope_t), ("class:muted", sep), ("class:text", mode)]
        if show_model:
            row += [("class:muted", sep), ("class:muted", model)]
        used = row_width(row)
        row.append(("", " " * max(cols - used - cw(tag) - 1, 1)))
        row.append(("class:attention", tag))
        return row

    def footer_row(self, cols: int) -> Row:
        ready, attn = self.world.ready_files(), self.world.attention()
        parts: list[tuple[str, str]] = [("class:text", f" {ready} files ready"), ("class:muted", " · "), ("class:attention" if attn else "class:muted", f"{attn} need attention")]
        j = self.job
        if j and j.active:
            label = "Stopping" if j.state == "stopping" else j.verb
            parts += [("class:muted", " · "), ("class:accent", f"{label} {j.source_name} {j.file_index}/{j.total}")]
        elif self.query_op and self.query_op.active:
            parts += [("class:muted", " · "), ("class:accent", self.query_op.stage)]
        right = "F1 / Commands "
        if self.hint_text:
            parts += [("class:muted", " · "), ("class:attention", self.hint_text)]
        left = parts
        if row_width(left) + cw(right) + 1 > cols:
            # drop the middle segments, keep counts and the commands hint
            left = parts[:3]
            if row_width(left) + cw(right) + 1 > cols:
                left = [("class:text", f" {ready} ready"), ("class:muted", " · "), ("class:attention" if attn else "class:muted", f"{attn} attention")]
                right = "F1 "
        gap = max(cols - row_width(left) - cw(right), 1)
        return left + [("", " " * gap), ("class:muted", right)]

    def header_fragments(self) -> FormattedText:
        return self._fix(self.header_rows(self.size()[0]))

    def footer_fragments(self) -> FormattedText:
        return self._fix(self.footer_row(self.size()[0]))

    def notice_fragments(self) -> FormattedText:
        return self._fix([("class:attention", " " + self.readiness_notice())])

    def transcript_fragments(self) -> FormattedText:
        m = self.metrics()
        rows = self.transcript_rows(m.text_w)
        h = m.transcript_h
        total = len(rows)
        max_scroll = max(total - h, 0)
        self.scroll = max(0, min(self.scroll, max_scroll))
        if self.scroll == 0:
            self.unread = False
        start = max(total - h - self.scroll, 0)
        view = rows[start : start + h]
        if self.unread and self.scroll > 0 and view:
            view = view[:-1] + [[("class:attention", "New messages — press Ctrl+End to go to latest")]]
        frags: list[tuple[str, str]] = []
        for i, r in enumerate(view):
            frags.append(("", "  "))
            frags.extend(r)
            if i < len(view) - 1:
                frags.append(("", "\n"))
        return self._fix(frags)

    def composer_top_fragments(self) -> FormattedText:
        cols = self.size()[0]
        g = self.glyphs
        title = " Ask "
        if self.job and self.job.active:
            title = " Ask (sending is paused while indexing) "
        elif self.query_op and self.query_op.active:
            title = " Ask (answering...) "
        title = title if cw(title) < cols - 4 else " Ask "
        fill = max(cols - 2 - cw(title) - 1, 0)
        return self._fix([("class:border", g.tl + g.h), ("class:muted", title), ("class:border", g.h * fill + g.tr)])

    def composer_bottom_fragments(self) -> FormattedText:
        cols = self.size()[0]
        g = self.glyphs
        return self._fix([("class:border", g.bl + g.h * max(cols - 2, 0) + g.br)])

    def overlay_hmax(self) -> int:
        """Rows an overlay may use.

        With room to spare (24+ rows) the overlay stays inside the transcript
        area so the composer and footer remain visible; on short terminals it
        may cover the composer but keeps the header and footer.
        """
        m = self.metrics()
        if m.rows >= 24:
            return m.transcript_h + m.notice_rows
        return max(m.rows - 2, 4)

    def overlay_fills(self) -> bool:
        cols, rows = self.size()
        return rows < 24 or cols < 80

    def overlay_rows(self) -> list[Row]:
        if not self.overlays:
            return []
        cols = self.size()[0]
        return self.overlays[-1].compose(ov.overlay_width(cols), self.overlay_hmax(), self.glyphs, self.overlay_fills())

    def overlay_top(self) -> int:
        return 1 + max((self.overlay_hmax() - len(self.overlay_rows())) // 2, 0)

    def overlay_fragments(self) -> FormattedText:
        rows = self.overlay_rows()
        frags: list[tuple[str, str]] = []
        for i, r in enumerate(rows):
            frags.extend(r)
            if i < len(rows) - 1:
                frags.append(("", "\n"))
        return self._fix(frags)

    def too_small_fragments(self) -> FormattedText:
        cols, rows = self.size()
        lines = [
            ("class:attention class:bold", "Terminal too small"),
            ("class:text", f"Docket needs at least {MIN_COLS}x{MIN_ROWS}; this is {cols}x{rows}."),
            ("class:muted", "Resize the window to continue. Your draft is kept."),
            ("class:muted", "Or run `docket chat --plain`."),
        ]
        out: list[tuple[str, str]] = []
        for s, t in lines:
            out.append((s, t))
            out.append(("", "\n"))
        return self._fix(out)

    # ------------------------------------------------------------------
    # application construction
    # ------------------------------------------------------------------
    def _build_application(self, input: Any, output: Any) -> None:
        not_small = Condition(lambda: not self.too_small())
        small = Condition(self.too_small)
        has_notice = Condition(lambda: bool(self.readiness_notice()) and self.size()[1] >= 20)
        has_overlay = Condition(lambda: bool(self.overlays))
        self._has_overlay = has_overlay

        header = Window(FormattedTextControl(self.header_fragments), height=1, style="class:header")
        notice = ConditionalContainer(Window(FormattedTextControl(self.notice_fragments), height=1, style="class:canvas"), filter=has_notice)
        transcript = Window(
            FormattedTextControl(self.transcript_fragments),
            height=lambda: Dimension.exact(self.metrics().transcript_h),
            style="class:canvas",
            wrap_lines=False,
        )
        top = Window(FormattedTextControl(self.composer_top_fragments), height=1, style="class:canvas")
        self.composer_window = Window(
            BufferControl(
                self.composer,
                input_processors=[
                    ConditionalProcessor(
                        BeforeInput(lambda: self._fix([("", "Ask about your documents...")]), style="class:muted"),
                        Condition(lambda: not self.composer.text),
                    )
                ],
            ),
            height=lambda: Dimension.exact(self.metrics().composer_lines),
            wrap_lines=True,
            style="class:canvas",
        )
        side = lambda: Window(width=1, char=lambda: self.glyphs.v, style="class:border")  # noqa: E731
        pad = lambda: Window(width=1, style="class:canvas")  # noqa: E731
        body = VSplit([side(), pad(), self.composer_window, pad(), side()], height=lambda: Dimension.exact(self.metrics().composer_lines))
        bottom = Window(FormattedTextControl(self.composer_bottom_fragments), height=1, style="class:canvas")
        footer = Window(FormattedTextControl(self.footer_fragments), height=1, style="class:footer")
        main = ConditionalContainer(HSplit([header, notice, transcript, top, body, bottom, footer]), filter=not_small)
        too_small = ConditionalContainer(
            Window(FormattedTextControl(self.too_small_fragments), style="class:canvas"), filter=small
        )
        self.overlay_window = Window(
            FormattedTextControl(self.overlay_fragments, focusable=True, show_cursor=False),
            style="class:panel",
            wrap_lines=False,
        )
        overlay_container = ConditionalContainer(self.overlay_window, filter=has_overlay & not_small)
        float_ = _DynamicTopFloat(
            self.overlay_top,
            content=overlay_container,
            width=lambda: ov.overlay_width(self.size()[0]),
            height=lambda: max(len(self.overlay_rows()), 1),
        )
        root = FloatContainer(content=HSplit([main, too_small], style="class:canvas"), floats=[float_])
        layout = Layout(root, focused_element=self.composer_window)

        def get_style():
            t = self.theme
            if t not in self._style_cache:
                self._style_cache[t] = make_style(t)
            return self._style_cache[t]

        self.application = Application(
            layout=layout,
            key_bindings=self._key_bindings(has_overlay),
            style=DynamicStyle(get_style),
            full_screen=True,
            mouse_support=False,
            input=input,
            output=output,
        )
        self.application.ttimeoutlen = 0.05
        self.application.timeoutlen = 0.3

    def _key_bindings(self, has_overlay: Condition) -> KeyBindings:
        kb = KeyBindings()
        no_overlay = ~has_overlay
        composer_empty = Condition(lambda: not self.composer.text)

        @kb.add("c-c", eager=True)
        def _ctrl_c(event):
            self.on_ctrl_c()

        @kb.add("c-d", eager=True)
        def _ctrl_d(event):
            self.request_exit()

        @kb.add("f1", eager=True)
        def _f1(event):
            self.open_palette()

        @kb.add("/", filter=no_overlay & composer_empty)
        def _slash(event):
            self.open_palette()

        for key, fn in (
            ("f2", lambda: self.push(ov.SourcesOverlay(self))),
            ("f3", lambda: self.push(ov.ScopeOverlay(self))),
            ("f4", lambda: self.push(ov.ModeOverlay(self))),
            ("f5", lambda: self.open_evidence(None, 0)),
            ("f6", lambda: self.cmd_details("")),
            ("f7", lambda: self.cmd_jobs("")),
            ("f8", lambda: self.push(ov.SettingsOverlay(self))),
        ):
            kb.add(key, eager=True)(lambda event, fn=fn: fn())

        # composer
        @kb.add("enter", filter=no_overlay)
        def _enter(event):
            self.submit()

        @kb.add("escape", "enter", filter=no_overlay)
        @kb.add("c-j", filter=no_overlay)
        def _newline(event):
            self.composer.insert_text("\n")

        @kb.add("pageup", filter=no_overlay)
        def _pgup(event):
            self.scroll += max(self.metrics().transcript_h - 2, 1)
            self.invalidate()

        @kb.add("pagedown", filter=no_overlay)
        def _pgdn(event):
            self.scroll = max(self.scroll - max(self.metrics().transcript_h - 2, 1), 0)
            self.invalidate()

        @kb.add("c-end", filter=no_overlay)
        def _latest(event):
            self.scroll = 0
            self.unread = False
            self.invalidate()

        @kb.add("escape", "up", filter=no_overlay)
        def _prev_answer(event):
            self._move_focus_answer(-1)

        @kb.add("escape", "down", filter=no_overlay)
        def _next_answer(event):
            self._move_focus_answer(1)

        # overlays
        @kb.add("escape", filter=has_overlay, eager=True)
        def _esc(event):
            self.close_top()

        def overlay_key(name: str):
            def handler(event):
                if self.overlays:
                    self.overlays[-1].key(name)
                    self.invalidate()

            return handler

        for k in ("up", "down", "left", "right", "pageup", "pagedown", "home", "end", "tab", "s-tab", "enter", "backspace", "c-u"):
            kb.add(k, filter=has_overlay)(overlay_key(k))

        @kb.add(Keys.BracketedPaste, filter=has_overlay)
        def _paste(event):
            if self.overlays:
                self.overlays[-1].text(event.data)
                self.invalidate()

        @kb.add(Keys.Any, filter=has_overlay)
        def _any(event):
            data = event.data
            if self.overlays and len(data) == 1 and data.isprintable():
                self.overlays[-1].text(data)
                self.invalidate()

        return kb

    # ------------------------------------------------------------------
    # focus / overlay stack
    # ------------------------------------------------------------------
    def invalidate(self) -> None:
        try:
            self.application.invalidate()
        except Exception:  # not running yet
            pass

    def _sync_focus(self) -> None:
        layout = self.application.layout
        target = self.overlay_window if self.overlays else self.composer_window
        if layout.current_window is not target:
            layout.focus(target)

    def push(self, overlay: ov.Overlay) -> None:
        if self.overlays and self.overlays[-1].name == overlay.name and overlay.name != "confirm":
            return
        self.overlays.append(overlay)
        self.hint_text = ""
        self._sync_focus()
        self.invalidate()

    def close_top(self, force: bool = False) -> None:
        if not self.overlays:
            return
        top = self.overlays[-1]
        if not force and not top.can_close():
            self.push(
                ov.ConfirmOverlay(
                    self,
                    "Discard changes?",
                    "You have unsaved settings. Discard them and close?",
                    [("Discard changes", lambda: self.close_top(force=True)), ("Keep editing", None)],
                    default=1,
                )
            )
            return
        self.overlays.pop()
        self._sync_focus()
        self.invalidate()

    def open_palette(self) -> None:
        self.push(ov.PaletteOverlay(self))

    def open_evidence(self, answer_pos: int | None, cit: int = 0) -> None:
        answers = self.answers()
        if not answers:
            self.notice("There is no answer yet, so there is no evidence to show.", "attention")
            return
        if answer_pos is None:
            answer_pos = self.focus_answer if 0 <= self.focus_answer < len(answers) else len(answers) - 1
        if not answers[answer_pos].answer.citations:
            self.notice("That answer has no citations. Open Answer details (F6) to see why.", "attention")
            return
        self.push(ov.EvidenceOverlay(self, answer_pos, cit))

    def _move_focus_answer(self, d: int) -> None:
        n = len(self.answers())
        if n:
            cur = self.focus_answer if self.focus_answer >= 0 else n - 1
            self.focus_answer = max(0, min(cur + d, n - 1))
            self.invalidate()

    # ------------------------------------------------------------------
    # operations (fake)
    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool((self.job and self.job.active) or (self.query_op and self.query_op.active))

    def start_job(self, src: fd.FakeSource, verb: str, on_done: str) -> None:
        if self.busy:
            self.notice("Another operation is running. Wait for it, or press Ctrl+C to stop it.", "attention")
            return
        self.job = IndexJob(src.id, src.name, verb, total=max(len(src.files), 1), on_done=on_done)
        self.push(ov.IndexingOverlay(self))

    def add_folder(self, path: str) -> None:
        src = self.world.new_source_from_path(path)
        self.notice(f"Added {src.name}. Indexing started.")
        self.start_job(src, "Indexing", "")

    def disconnect_source(self, source_id: str) -> None:
        s = self.world.find(source_id)
        if s:
            s.status = fd.DISCONNECTED
            s.note = "Disconnected by you. Stored originals are kept; it is not searched."
            if self.scope.source_id == source_id:
                self.notice(f"The selected scope {s.name} is disconnected. It stays selected; change scope to search elsewhere.", "attention")
            self.notice(f"{s.name} disconnected (demo). Its files are kept.")

    def stop_job(self) -> None:
        if self.job and self.job.active:
            self.job.request_stop()
            self.invalidate()

    def tick(self) -> None:
        """Advance fake operations by one step (timer, or `N` in reduced motion)."""
        j = self.job
        if j and j.active:
            j.advance(blocked=self.blocked)
            if not j.active:
                self._job_finished(j)
        q = self.query_op
        if q and q.active:
            q.advance()
            if q.state == "done":
                self._finish_query(q)
            elif q.state == "cancelled":
                self.query_op = None
                self.notice("Question cancelled. Nothing was added to the conversation. Use /retry to ask again.", "attention")
        self.invalidate()

    def _job_finished(self, j: IndexJob) -> None:
        src = self.world.find(j.source_id)
        if j.state == "done" and src and j.on_done == "ready":
            src.status, src.note = fd.READY, ""
        if j.state == "done":
            level = "attention" if j.failed else "info"
            self.notice(f"Indexing {j.source_name} finished: {j.indexed} indexed, {j.unchanged} unchanged, {j.failed} failed.", level)
        elif j.state == "stopped":
            self.notice(f"Indexing {j.source_name} stopped after {j.file_index} of {j.total} files. Completed files were kept.", "attention")
        else:
            self.notice(f"Indexing {j.source_name} failed: {j.reason} The folder stays registered; retry after repair.", "error")

    def _finish_query(self, q: QueryOp) -> None:
        a = fd.answer_for(q.question, q.scope_label, q.mode_label)
        self.query_op = None
        self.add_message(Message("assistant", a.text, answer=a, mode_label=a.mode_label, elapsed=a.elapsed, scope_label=a.scope_label))
        self.focus_answer = len(self.answers()) - 1
        self.invalidate()

    def ask(self, question: str) -> bool:
        if self.busy:
            what = "indexing" if self.job and self.job.active else "answering"
            self.notice(f"Sending is unavailable while {what}. Your draft is kept; press Ctrl+C to stop, or wait.", "attention")
            return False
        if self.world.ready_files() == 0:
            self.notice("Nothing is searchable yet. Add a folder first (F2).", "attention")
            return False
        self.last_question = question
        self.add_message(Message("user", question))
        op = QueryOp(question, scope_label=self.scope_label(), mode_label=self.mode_label())
        if self.reduced_motion:
            self._finish_query(op)
        else:
            self.query_op = op
        self.scroll = 0
        self.invalidate()
        return True

    # ------------------------------------------------------------------
    # input handling
    # ------------------------------------------------------------------
    def _clear_hint(self) -> None:
        if self.hint_text:
            self.hint_text = ""

    def on_ctrl_c(self) -> None:
        if self.overlays:
            self.close_top()
        elif self.busy:
            if self.job and self.job.active:
                self.stop_job()
            elif self.query_op:
                self.query_op.state = "stopping"
            self.invalidate()
        elif self.composer.text:
            self.composer.reset()
            self.invalidate()
        else:
            self.hint_text = "Press Ctrl+D to exit"
            self.invalidate()

    def request_exit(self) -> None:
        if self.busy:
            self.push(
                ov.ConfirmOverlay(
                    self,
                    "Exit while work is running?",
                    "Indexing or an answer is still in progress. Docket does not keep running after it exits.",
                    [("Stop and exit", self._stop_and_exit), ("Keep running", None)],
                    default=1,
                )
            )
        else:
            self.application.exit()

    def _stop_and_exit(self) -> None:
        self.application.exit()

    def submit(self) -> None:
        text = self.composer.text.strip()
        if not text:
            return
        if text.startswith("/") or text.lower() in BARE_WORDS:
            self.composer.reset()
            self.run_text(text)
            self.invalidate()
            return
        if self.ask(text):
            self.composer.reset()

    def run_text(self, text: str) -> None:
        """Run a typed slash command (or bare word) through the shared registry."""
        text = text.strip()
        if text.startswith("/"):
            token, _, arg = text[1:].partition(" ")
        else:
            token, arg = BARE_WORDS.get(text.lower(), text), ""
        cmd = self.registry.resolve(token)
        if cmd is None:
            hint = self.registry.suggest(token)
            tip = f" Did you mean /{hint}?" if hint else " Press F1 for commands."
            self.notice(f"Unknown command /{token}.{tip}", "attention")
        else:
            self._run(cmd, arg.strip())

    def _run(self, cmd: SlashCommand, arg: str) -> None:
        if cmd.handler(self, arg) is True:  # the registry's exit handler
            self.request_exit()

    def run_command(self, name: str, arg: str = "") -> None:
        cmd = self.registry.resolve(name)
        if cmd:
            self._run(cmd, arg)

    # ---- command handlers (same names as the plain interactive session) -
    def cmd_help(self, arg: str) -> None:
        self.open_palette()

    def cmd_sources(self, arg: str) -> None:
        self.push(ov.SourcesOverlay(self))

    def cmd_add(self, arg: str) -> None:
        path = arg.strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "'\"":
            path = path[1:-1]
        if not path:
            self.push(ov.AddFolderOverlay(self))
            return
        match = next((s for s in self.world.sources if s.path == path), None)
        if match and match.status == fd.DISCONNECTED:
            self.notice(f"{match.name} is disconnected. Use /reconnect {match.id} instead of adding it again.", "attention")
        elif match:
            self.notice(f"{match.name} is already registered.", "attention")
        else:
            self.add_folder(path)

    def _find_source(self, token: str) -> fd.FakeSource | None:
        t = token.strip().lower()
        return next((s for s in self.world.sources if t and (s.id.lower() == t or s.name.lower() == t or s.id.lower().endswith(t))), None)

    def cmd_ingest(self, arg: str) -> None:
        if arg.strip() in ("", "all"):
            target = next((s for s in self.world.sources if s.status != fd.DISCONNECTED), None)
        else:
            target = self._find_source(arg)
        if target is None:
            self.notice("No source to index. Use /add <folder> first.", "attention")
            return
        self.start_job(target, "Refreshing", "ready" if target.status == fd.FAILED else "")

    def cmd_mode(self, arg: str) -> None:
        token = arg.strip().lower()
        if not token:
            self.push(ov.ModeOverlay(self))
            return
        alias = {"auto": "auto", "fast": "fast", "quick": "fast", "plan": "plan", "agent": "plan"}
        key = alias.get(token)
        if key is None:
            self.notice(f"Unknown mode {token!r}. Choose auto, fast or plan.", "attention")
        elif key == "plan":
            self.notice("Plan mode is not available yet: planning is not implemented in the backend.", "attention")
        else:
            self.set_mode(key)

    def cmd_remove(self, arg: str) -> None:
        s = self._find_source(arg)
        if s is None:
            self.push(ov.SourcesOverlay(self))
            return
        self.push(
            ov.ConfirmOverlay(
                self,
                f"Disconnect {s.name}?",
                f"{s.name} will stop being searched. Originals and stored copies are kept. (Demo: nothing changes on disk.)",
                [("Disconnect", lambda: self.disconnect_source(s.id)), ("Keep connected", None)],
                default=1,
            )
        )

    def cmd_reconnect(self, arg: str) -> None:
        s = self._find_source(arg)
        if s is None or s.status != fd.DISCONNECTED:
            self.notice("Name a disconnected source, or open Sources (F2) and choose Reconnect.", "attention")
            if s is None:
                self.push(ov.SourcesOverlay(self))
            return
        self.start_job(s, "Reconnecting", "ready")

    def cmd_show(self, arg: str) -> None:
        answers = self.answers()
        if not answers:
            self.notice("There is no answer yet, so there is no evidence to show.", "attention")
            return
        try:
            n = int(arg.strip() or "1")
        except ValueError:
            self.notice("Usage: /show <n> where n is a citation number.", "attention")
            return
        cits = answers[-1].answer.citations
        if not 1 <= n <= len(cits):
            self.notice(f"The latest answer has {len(cits)} citation(s); choose 1-{len(cits)}.", "attention")
            return
        self.push(ov.EvidenceOverlay(self, len(answers) - 1, n - 1))

    def cmd_retry(self, arg: str) -> None:
        if not self.last_question:
            self.notice("There is no question to retry yet.", "attention")
            return
        self.ask(self.last_question)

    def cmd_status(self, arg: str) -> None:
        self.push(ov.SettingsOverlay(self, "system"))

    def cmd_clear(self, arg: str) -> None:
        self.messages = []
        self._line_cache.clear()
        self.scroll, self.unread, self.focus_answer, self.last_question = 0, False, -1, ""
        self.notice("Conversation cleared. Your typed-input history is kept.")

    def cmd_exit(self, arg: str) -> None:
        self.request_exit()

    def cmd_scope(self, arg: str) -> None:
        self.push(ov.ScopeOverlay(self))

    def cmd_jobs(self, arg: str) -> None:
        self.push(ov.JobsOverlay(self))

    def cmd_settings(self, arg: str) -> None:
        self.push(ov.SettingsOverlay(self))

    def cmd_details(self, arg: str) -> None:
        answers = self.answers()
        if not answers:
            self.notice("There is no answer yet, so there are no details to show.", "attention")
            return
        pos = self.focus_answer if 0 <= self.focus_answer < len(answers) else len(answers) - 1
        self.push(ov.DetailsOverlay(self, pos))

    def cmd_rechunk(self, arg: str) -> None:
        self.notice("Updating stored chunks arrives with the maintenance stage; it is not wired in this prototype.", "attention")

    def cmd_reindex(self, arg: str) -> None:
        self.notice("Rebuilding the index arrives with the maintenance stage; it is not wired in this prototype.", "attention")

    # ------------------------------------------------------------------
    # state changes used by overlays
    # ------------------------------------------------------------------
    def set_scope(self, scope: Scope) -> None:
        if scope == self.scope:
            return
        self.scope = scope
        self.notice(
            f"Scope is now {self.scope_label()}. Earlier answers keep their original scope; the next question starts with fresh context.",
        )

    def set_mode(self, key: str) -> None:
        if key == self.mode:
            return
        self.mode = key
        self.notice(f"Mode is now {self.mode_label()}.")

    def set_theme(self, name: str) -> None:
        self.theme = name
        self.invalidate()

    def settings_values(self) -> dict[str, Any]:
        return {"model": self.model, "mode": self.mode, "thinking": self.thinking, "theme": self.theme, "history": self.history_enabled}

    def apply_settings(self, values: dict[str, Any]) -> None:
        self.model = values["model"]
        self.thinking = values["thinking"]
        self.history_enabled = values["history"]
        self.set_theme(values["theme"])
        if values["mode"] != self.mode:
            self.set_mode(values["mode"])
        self.notice("Settings saved for this demo session (nothing is written to disk).")

    def palette_entries(self) -> list[ov.Entry]:
        extras = {n: why for n, _s, why in fd.COMMAND_EXTRAS}
        out: list[ov.Entry] = []
        for c in self.registry.all():
            why = extras.get(c.name, "")
            alias = " ".join(c.aliases)
            out.append(ov.Entry(c.name, c.usage, c.summary, "", ov.MUTED, not why, why, alias))
        return out

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------
    def _start_ticker(self) -> None:
        self.application.create_background_task(self._ticker())

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            if self.reduced_motion:
                continue
            if self.busy:
                self.tick()

    async def run_async(self) -> None:
        await self.application.run_async(pre_run=self._start_ticker, handle_sigint=False)

    def run(self) -> None:
        self.application.run(pre_run=self._start_ticker)
