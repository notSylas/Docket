"""Screen A prototype: one prompt_toolkit Application, fake data only.

Structure (spec section 6.2): views are the ``*_fragments`` methods and the
overlay classes; ``TuiApp`` is the interaction controller (commands, overlay
stack, drafts, operation ownership, worker threads); the "application/services"
layer is a ``TuiBackend`` (``backend.py``): ``FakeBackend`` for the demo,
``RealBackend`` for ``docket ui``. ``DemoUI`` is the demo wiring of the same
controller (fake backend, scripted starting states).
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from dataclasses import dataclass, replace
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
from docket.interfaces.cli.interactive.query_flow import shell_command_hint
from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui import overlays as ov
from docket.interfaces.cli.tui.backend import (
    AskOutcome,
    AskRequest,
    BackendError,
    FakeBackend,
    IndexOutcome,
    IndexProgress,
    ReadinessItem,
    ReadinessReport,
    SourceView,
    TuiBackend,
    Turn,
    describe_error,
)
from docket.interfaces.cli.tui.model import IndexJob, Message, QueryOp, Scope
from docket.interfaces.cli.tui.textfmt import (
    Row,
    cw,
    fit_row,
    indent_rows,
    render_markdown,
    row_width,
    truncate_row,
    unique_path_labels,
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


def build_registry(extras: tuple[tuple[str, str, str], ...] = fd.COMMAND_EXTRAS) -> CommandRegistry:
    """The existing registry (same names, aliases, prefix rules) plus Screen A additions."""
    reg = default_registry()
    for name, summary, _why in extras:
        reg.register(SlashCommand(name, summary, _call("cmd_" + name)))
    return reg


class TuiApp:
    def __init__(
        self,
        backend: TuiBackend,
        *,
        ascii_mode: bool | None = None,
        reduced_motion: bool = False,
        theme: str = "dark",
        input: Any = None,
        output: Any = None,
        tick_interval: float = 0.5,
    ) -> None:
        self.backend = backend
        self.ascii_mode = (
            detect_ascii(encoding=getattr(sys.stdout, "encoding", None)) if ascii_mode is None else ascii_mode
        )
        self.reduced_motion = reduced_motion
        self.theme = theme
        self.tick_interval = tick_interval
        self.glyphs = ASCII if self.ascii_mode else UNICODE

        self.world = backend.load_world()
        self.readiness: ReadinessReport = (
            backend.readiness()
            if backend.demo
            else ReadinessReport((ReadinessItem("Status", "Checking..."),), checking=True)
        )
        self.scope = Scope()
        self.mode = "auto"
        self.model = backend.default_model
        self.thinking = 1
        self.history_enabled = True
        self.messages: list[Message] = []
        self._uid = 0
        self.overlays: list[ov.Overlay] = []
        self.job: IndexJob | None = None
        self.query_op: QueryOp | None = None
        self.last_question = ""
        self.history: list[Turn] = []
        self.retry_turn_index: int | None = None
        self.scroll = 0  # lines above the bottom of the transcript
        self.unread = False
        self.focus_answer = -1
        self.hint_text = ""
        self.registry = build_registry(backend.command_extras)
        self._style_cache: dict[str, Any] = {}
        self._line_cache: dict[tuple, list[Row]] = {}
        # worker plumbing (real backends only)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._threads: list[threading.Thread] = []
        self._job_stop = threading.Event()
        self._readiness_running = False
        self._announced_blocked = False

        self._seed()
        self.composer = Buffer(multiline=True, name="composer", on_text_changed=lambda _b: self._clear_hint())
        self._build_application(input, output)
        self._sync_focus()

    # ------------------------------------------------------------------
    # seeding
    # ------------------------------------------------------------------
    def _seed(self) -> None:
        self.add_message(Message("system", self.backend.intro_notice))
        if not self.world.sources:
            self.overlays.append(ov.WelcomeOverlay(self))

    @property
    def blocked(self) -> bool:
        return self.readiness.blocked


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
            rows.extend(indent_rows(render_markdown(m.text, width - 2, ascii_mode=self.ascii_mode), "  "))
            a = m.answer
            if a and a.citations:
                rows.append([])
                rows.append([("class:muted", "  Sources:")])
                labels = unique_path_labels([c.rel_path for c in a.citations])
                for n, (c, label) in enumerate(zip(a.citations, labels), 1):
                    tail = [] if c.available else [("class:attention", "  (unavailable)")]
                    rows.append(truncate_row([("", "    "), ("class:chip", f"[{n}]"), ("class:text", f" {label}"), *tail], width))
            if a and a.warnings:
                rows.append([])  # a gap, so a warning never reads as one more source
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
            actions: list[tuple[str, str]] = []
            if a and a.citations:
                actions += [("class:chip", "[Evidence F5]"), (" ", " "), ("class:chip", "[Ctrl+E]"), (" ", " ")]
            actions.append(("class:chip", "[Details F6]"))
            rows.extend(indent_rows(wrap_tokens([("class:muted", " · ".join(meta) + "   ")] + actions, width - 2), "  "))
            if a and a.citations:
                rows.extend(
                    indent_rows(
                        wrap_tokens(
                            inline_tokens(
                                "Open a source: /show 1" + (f" to /show {len(a.citations)}" if len(a.citations) > 1 else ""),
                                "class:muted",
                            ),
                            width - 2,
                        ),
                        "  ",
                    )
                )
            if focused:
                note = "Selected answer: Enter opens evidence, Esc clears" if a and a.citations else "Selected answer: Esc clears"
                rows.extend(indent_rows([[("class:muted", note)]], "  "))
        else:
            label, style = {"info": ("Notice", "class:system"), "attention": ("Attention", "class:attention"), "error": ("Failed", "class:error")}[m.level]
            rows.extend(wrap_tokens([(style + " class:bold", label + ": ")] + inline_tokens(m.text, style), width, "  "))
        rows.append([])
        self._line_cache[key] = rows
        return rows

    def marker_visible(self) -> bool:
        """True while a specific answer is marked with the selected-answer arrow."""
        answers = self.answers()
        return len(answers) > 1 and 0 <= self.focus_answer < len(answers)

    def transcript_rows(self, width: int) -> list[Row]:
        answers = self.answers()
        focus_uid = answers[self.focus_answer].uid if self.marker_visible() else -1
        out: list[Row] = []
        for m in self.messages:
            out.extend(self._render_message(m, width, m.uid == focus_uid))
        q = self.query_op
        if q and q.active:
            if q.state == "stopping" and q.real:
                out.append([("class:attention", "Cancelling: the model call cannot be interrupted; its answer will be discarded when it finishes.")])
            elif q.state == "stopping":
                out.append([("class:attention", "Stopping after the current operation...")])
            else:
                tail = f"  {q.elapsed_secs}s" if q.real else ""
                out.append([("class:accent", f"{q.stage}..."), ("class:muted", f"{tail}  Ctrl+C to cancel")])
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
        return next(lbl for k, lbl, *_ in self.backend.modes if k == self.mode)

    def header_rows(self, cols: int) -> Row:
        scope, mode, model = self.scope_label(), self.mode_label(), self.model
        tag = fd.DEMO_TAG if self.backend.demo else ""
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
        elif self.answers() and not self.overlays:
            # keyboard routes to the evidence; each hint is dropped whole when it will not fit
            hints = ["Ctrl+E evidence · /show N"] if any(m.answer.citations for m in self.answers()) else []
            if self.marker_visible():
                cited = bool(self.answers()[self.focus_answer].answer.citations)
                hints.insert(0, "Enter opens evidence, Esc clears" if cited else "Esc clears the selection")
            for h in hints:
                trial = parts + [("class:muted", " · "), ("class:muted", h)]
                if row_width(trial) + cw(right) + 1 <= cols:
                    parts = trial
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
        if self.overlays:
            # A modal sits on a clean backdrop: no half-hidden transcript beside it.
            return self._fix([("", "\n" * max(m.transcript_h - 1, 0))])
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

        def add(*keys: Any, **kw: Any):
            """kb.add, but a failing handler becomes a notice instead of a crash."""

            def deco(fn):
                def guarded(event):
                    try:
                        fn(event)
                    except Exception as exc:  # noqa: BLE001
                        self._internal_error(exc)
                        self.invalidate()

                return kb.add(*keys, **kw)(guarded)

            return deco
        no_overlay = ~has_overlay
        composer_empty = Condition(lambda: not self.composer.text)

        @add("c-c", eager=True)
        def _ctrl_c(event):
            self.on_ctrl_c()

        @add("c-d", eager=True)
        def _ctrl_d(event):
            self.request_exit()

        @add("f1", eager=True)
        def _f1(event):
            self.open_palette()

        @add("/", filter=no_overlay & composer_empty)
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
            add(key, eager=True)(lambda event, fn=fn: fn())

        # composer
        @add("enter", filter=no_overlay)
        def _enter(event):
            self.submit()

        @add("escape", "enter", filter=no_overlay)
        @add("c-j", filter=no_overlay)
        def _newline(event):
            self.composer.insert_text("\n")

        @add("pageup", filter=no_overlay)
        def _pgup(event):
            self.scroll += max(self.metrics().transcript_h - 2, 1)
            self.invalidate()

        @add("pagedown", filter=no_overlay)
        def _pgdn(event):
            self.scroll = max(self.scroll - max(self.metrics().transcript_h - 2, 1), 0)
            self.invalidate()

        @add("c-end", filter=no_overlay)
        def _latest(event):
            self.scroll = 0
            self.unread = False
            self.invalidate()

        @add("c-e", filter=no_overlay)
        def _ctrl_e(event):
            self.open_evidence(None, 0)

        marker_on = Condition(self.marker_visible)

        @add("escape", filter=no_overlay & marker_on)
        def _clear_marker(event):
            self.focus_answer = -1
            self.invalidate()

        @add("escape", "up", filter=no_overlay)
        def _prev_answer(event):
            self._move_focus_answer(-1)

        @add("escape", "down", filter=no_overlay)
        def _next_answer(event):
            self._move_focus_answer(1)

        # overlays
        @add("escape", filter=has_overlay, eager=True)
        def _esc(event):
            self.close_top()

        def overlay_key(name: str):
            def handler(event):
                if self.overlays:
                    self.overlays[-1].key(name)
                    self.invalidate()

            return handler

        for k in ("up", "down", "left", "right", "pageup", "pagedown", "home", "end", "tab", "s-tab", "enter", "backspace", "c-u"):
            add(k, filter=has_overlay)(overlay_key(k))

        @add(Keys.BracketedPaste, filter=has_overlay)
        def _paste(event):
            if self.overlays:
                self.overlays[-1].text(event.data)
                self.invalidate()

        @add(Keys.Any, filter=has_overlay)
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
    # operations
    #
    # Demo backend: simulated by the timer (``tick``). Real backend: one
    # worker thread per expensive operation; workers never touch the UI, they
    # publish immutable snapshots through ``_post`` (call_soon_threadsafe).
    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool((self.job and self.job.active) or (self.query_op and self.query_op.active))

    def _post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the UI loop. Safe from any thread; dropped once the UI is gone."""
        loop = self._loop
        if loop is None or self._closed:
            return

        def run() -> None:
            if self._closed:
                return
            try:
                fn()
            except Exception as exc:  # a UI-side callback must never take the app down
                self._internal_error(exc)
            self.invalidate()

        try:
            loop.call_soon_threadsafe(run)
        except RuntimeError:  # loop already closed
            pass

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        t = threading.Thread(target=target, name=f"docket-ui-{name}", daemon=True)
        self._threads.append(t)
        t.start()

    def _internal_error(self, exc: BaseException) -> None:
        self.notice(f"Unexpected error ({type(exc).__name__}): {exc}", "error")

    def active_workers(self) -> list[threading.Thread]:
        """Indexing workers still running (an answer in flight is abandoned on exit)."""
        return [t for t in self._threads if t.is_alive() and t.name == "docket-ui-index"]

    def wait_for_workers(self, timeout: float | None = None) -> bool:
        """Join indexing workers after the UI exits (they stop after the current file)."""
        for t in self.active_workers():
            t.join(timeout)
        return not self.active_workers()

    def refresh_world(self) -> None:
        self.world = self.backend.load_world()
        self.invalidate()

    # ---- readiness --------------------------------------------------------
    def refresh_readiness(self) -> None:
        if self.backend.demo:
            self.readiness = self.backend.readiness()
            self.invalidate()
            return
        if self._readiness_running:
            return
        self._readiness_running = True
        self.readiness = replace(self.readiness, checking=True, message="Checking...")

        def work() -> None:
            try:
                report = self.backend.readiness()
            except Exception as exc:  # noqa: BLE001 -- a check must not crash the UI
                text = (str(exc) or type(exc).__name__).splitlines()[0][:120]
                report = ReadinessReport((ReadinessItem("Status", f"Could not check: {text}", True),), message="The readiness check failed.")
            self._post(lambda: self._readiness_arrived(report))

        self._spawn("readiness", work)

    def _readiness_arrived(self, report: ReadinessReport) -> None:
        self._readiness_running = False
        self.readiness = report
        if report.blocked and not self._announced_blocked:
            self._announced_blocked = True
            self.notice(f"Not ready: {report.help or 'a required service or model is missing.'} Open Settings (F8) for details.", "attention")

    # ---- indexing ---------------------------------------------------------
    def start_job(self, src: SourceView, verb: str, on_done: str) -> None:
        if self.busy:
            self.notice("Another operation is running. Wait for it, or press Ctrl+C to stop it.", "attention")
            return
        if self.backend.demo:
            self.job = IndexJob(src.id, src.name, verb, total=max(len(src.files), 1), on_done=on_done)
        else:
            self._start_real_job(src, verb, on_done)
        self.push(ov.IndexingOverlay(self))

    def _start_real_job(self, src: SourceView, verb: str, on_done: str) -> None:
        job = IndexJob(src.id, src.name, verb, total=0, on_done=on_done, real=True, started=time.monotonic())
        self.job = job
        stop = threading.Event()
        self._job_stop = stop

        def on_progress(p: IndexProgress) -> None:
            self._post(lambda: self._apply_progress(job, p))

        def work() -> None:
            try:
                outcome = self.backend.run_index(src.id, on_progress, stop.is_set)
            except Exception as exc:  # noqa: BLE001 -- surfaced as a failed job
                outcome = IndexOutcome("failed", IndexProgress(), reason=describe_error(exc))
            self._post(lambda: self._real_job_done(job, outcome))

        self._spawn("index", work)

    def _apply_progress(self, job: IndexJob, p: IndexProgress) -> None:
        if job is not self.job or not job.active:
            return
        job.file_index, job.total, job.current_name = p.index, p.total, p.current
        job.indexed, job.unchanged, job.failed = p.indexed, p.unchanged, p.failed
        job.failures = list(p.failures)

    def _real_job_done(self, job: IndexJob, outcome: IndexOutcome) -> None:
        if job is not self.job:
            return
        p = outcome.progress
        job.file_index, job.total = p.index, max(p.total, p.index)
        job.indexed, job.unchanged, job.failed = p.indexed, p.unchanged, p.failed
        job.failures = list(p.failures)
        job.state, job.reason, job.ended = outcome.state, outcome.reason, time.monotonic()
        job.notices = outcome.notices
        self._job_finished(job)

    def add_folder(self, path: str) -> None:
        try:
            src = self.backend.add_source(path)
        except BackendError as exc:
            self.notice(str(exc), "error")
            return
        self.refresh_world()
        src = self.world.find(src.id) or src
        self.overlays = [o for o in self.overlays if o.name != "welcome"]  # it is stale once a folder exists
        self.notice(f"Added {src.name}. Indexing started.")
        self.start_job(src, "Indexing", "")

    def reconnect_source(self, src: SourceView) -> None:
        if self.busy:
            self.notice("Another operation is running. Wait for it, or press Ctrl+C to stop it.", "attention")
            return
        try:
            self.backend.reconnect_source(src.id)
        except BackendError as exc:
            self.notice(str(exc), "error")
            return
        self.refresh_world()
        self.start_job(self.world.find(src.id) or src, "Reconnecting", "ready")

    def disconnect_source(self, source_id: str) -> None:
        s = self.world.find(source_id)
        if s is None:
            return
        if self.job and self.job.active and self.job.source_id == source_id:
            self.notice(f"{s.name} is being indexed. Stop indexing first, then disconnect it.", "attention")
            return
        try:
            self.backend.disconnect_source(source_id)
        except BackendError as exc:
            self.notice(str(exc), "error")
            return
        self.refresh_world()
        if self.scope.source_id == source_id:
            self.notice(f"The selected scope {s.name} is disconnected. It stays selected; change scope to search elsewhere.", "attention")
        self.notice(f"{s.name} disconnected{' (demo)' if self.backend.demo else ''}. Its files are kept.")

    def stop_job(self) -> None:
        if self.job and self.job.active:
            self.job.request_stop()
            self._job_stop.set()
            self.invalidate()

    def tick(self) -> None:
        """Advance simulated operations by one step (timer, or `N` in reduced motion).

        Real operations are driven by their worker; for them a tick only
        redraws (so the elapsed time moves).
        """
        j = self.job
        if j and j.active and not j.real:
            j.advance(blocked=self.blocked)
            if not j.active:
                self._job_finished(j)
        q = self.query_op
        if q and q.active and not q.real:
            q.advance()
            if q.state == "done":
                self._finish_query(q)
            elif q.state == "cancelled":
                self.query_op = None
                self.notice("Question cancelled. Nothing was added to the conversation. Use /retry to ask again.", "attention")
        self.invalidate()

    def _job_finished(self, j: IndexJob) -> None:
        if j.state == "done":
            self.backend.job_finished(j.source_id, j.on_done)
        self.refresh_world()
        if j.state == "done":
            level = "attention" if j.failed else "info"
            self.notice(f"Indexing {j.source_name} finished: {j.indexed} indexed, {j.unchanged} unchanged, {j.failed} failed.", level)
        elif j.state == "stopped":
            self.notice(f"Indexing {j.source_name} stopped after {j.file_index} of {j.total} files. Completed files were kept.", "attention")
        else:
            self.notice(f"Indexing {j.source_name} failed: {j.reason} The folder stays registered; retry after repair.", "error")
        for line in j.notices:
            self.notice(line)

    # ---- asking -----------------------------------------------------------
    def _history_for(self, q: QueryOp) -> tuple[Turn, ...]:
        if q.retry and self.retry_turn_index is not None:
            return tuple(self.history[: self.retry_turn_index])
        return tuple(self.history)

    def _set_stage(self, q: QueryOp, label: str) -> None:
        if q is self.query_op and q.active:
            q.stage_name = label

    def _finish_query(self, q: QueryOp, outcome: AskOutcome | None = None) -> None:
        if outcome is None:  # the demo: answer synchronously when its simulated stages end
            outcome = self.backend.ask(AskRequest(q.question, self.mode, self._history_for(q)), lambda _s: None, q.scope_label, q.mode_label)
        if q is not self.query_op and not self.backend.demo:
            return
        cancelled = q.state == "stopping" and q.real
        self.query_op = None
        if cancelled:
            self.notice("Question cancelled. Its answer was discarded and nothing was added to the conversation. Use /retry to ask again.", "attention")
            return
        if outcome.error or outcome.answer is None:
            self.notice(f"{outcome.error or 'No answer was produced.'} Your question is kept; use /retry to ask again.", "error")
            return
        a = outcome.answer
        self.add_message(Message("assistant", a.text, answer=a, mode_label=a.mode_label, elapsed=a.elapsed, scope_label=a.scope_label, history_answer=outcome.history_answer))
        turn = Turn(q.question, outcome.history_answer or a.text)
        idx = self.retry_turn_index
        if q.retry and idx is not None and idx < len(self.history):
            self.history[idx] = turn
        else:
            self.retry_turn_index = len(self.history)
            self.history.append(turn)
        self.focus_answer = len(self.answers()) - 1
        self.invalidate()

    def ask(self, question: str, retry: bool = False) -> bool:
        hint = None if retry else shell_command_hint(question)
        if hint is not None:
            self.notice(hint, "attention")
            return False
        if self.busy:
            what = "indexing" if self.job and self.job.active else "answering"
            self.notice(f"Sending is unavailable while {what}. Your draft is kept; press Ctrl+C to stop, or wait.", "attention")
            return False
        if self.world.ready_files() == 0:
            self.notice("Nothing is searchable yet. Add a folder first (F2).", "attention")
            return False
        self.last_question = question
        if not retry:
            self.retry_turn_index = None
        self.add_message(Message("user", question))
        real = not self.backend.demo
        op = QueryOp(
            question,
            scope_label=self.scope_label(),
            mode_label=self.mode_label(),
            real=real,
            started=time.monotonic() if real else None,
            stage_name=self.backend.stage_search if real else "",
            retry=retry,
        )
        if real:
            self.query_op = op
            self._start_real_ask(op)
        elif self.reduced_motion:
            self._finish_query(op)
        else:
            self.query_op = op
        self.scroll = 0
        self.invalidate()
        return True

    def _start_real_ask(self, op: QueryOp) -> None:
        request = AskRequest(op.question, self.mode, self._history_for(op))

        def on_stage(label: str) -> None:
            self._post(lambda: self._set_stage(op, label))

        def work() -> None:
            try:
                outcome = self.backend.ask(request, on_stage, op.scope_label, op.mode_label)
            except Exception as exc:  # noqa: BLE001 -- surfaced as a failed question
                outcome = AskOutcome(error=describe_error(exc))
            self._post(lambda: self._finish_query(op, outcome))

        self._spawn("ask", work)

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
        # A real indexing run stops after its current file; the entry point
        # waits for that before returning to the shell (see run_ui).
        if self.job and self.job.active:
            self.stop_job()
        self.application.exit()

    def submit(self) -> None:
        text = self.composer.text.strip()
        if not text:
            if self.marker_visible():
                self.open_evidence(self.focus_answer, 0)
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
        check = self.backend.check_folder(path)
        if not check.ok:
            self.notice(check.message, "attention")
        else:
            self.add_folder(check.path)

    def _find_source(self, token: str) -> SourceView | None:
        t = token.strip().lower()
        return next((s for s in self.world.sources if t and (s.id.lower() == t or s.name.lower() == t or s.id.lower().endswith(t))), None)

    def cmd_ingest(self, arg: str) -> None:
        if arg.strip() in ("", "all"):
            target = next((s for s in self.world.sources if s.status in (fd.READY, fd.FAILED)), None)
        else:
            target = self._find_source(arg)
        if target is None:
            self.notice("No source to index. Use /add <folder> first.", "attention")
            return
        if target.status not in (fd.READY, fd.FAILED):
            self.notice(f"{target.name} is {fd.STATUS_LABEL[target.status].lower()}; it cannot be indexed. See Sources (F2).", "attention")
            return
        self.start_job(target, "Refreshing", "ready" if target.status == fd.FAILED else "")

    def cmd_mode(self, arg: str) -> None:
        token = arg.strip().lower()
        if not token:
            self.push(ov.ModeOverlay(self))
            return
        keys = {k for k, *_ in self.backend.modes}
        alias = {"auto": "auto", "fast": "fast", "quick": "fast", "plan": "plan", "agent": "agent" if "agent" in keys else "plan"}
        key = alias.get(token)
        if key is None:
            self.notice(f"Unknown mode {token!r}. Choose {', '.join(sorted(keys))}.", "attention")
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
                f"{s.name} will stop being searched. Originals and stored copies are kept." + (" (Demo: nothing changes on disk.)" if self.backend.demo else ""),
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
        self.reconnect_source(s)

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
        if self.query_op and self.query_op.active:
            self.notice("An answer is being written. Wait for it, or press Ctrl+C, then clear.", "attention")
            return
        self.history = []
        self.retry_turn_index = None
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
        self.notice(self._maintenance_note("Updating stored chunks"), "attention")

    def cmd_reindex(self, arg: str) -> None:
        self.notice(self._maintenance_note("Rebuilding the index"), "attention")

    def _maintenance_note(self, what: str) -> str:
        if self.backend.demo:
            return f"{what} arrives with the maintenance stage; it is not wired in this prototype."
        return f"{what} is not available in this screen yet. Use the command line: docket ingest --all --rechunk, or docket reindex."

    # ------------------------------------------------------------------
    # state changes used by overlays
    # ------------------------------------------------------------------
    def set_scope(self, scope: Scope) -> None:
        if scope == self.scope:
            return
        if not self.backend.scope_enforced:
            self.notice(
                "Choosing a scope is not connected to search yet: questions still search all ready sources. "
                "The selection was not changed.",
                "attention",
            )
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
        self.notice(self.backend.settings_note)

    def palette_entries(self) -> list[ov.Entry]:
        extras = {n: why for n, _s, why in self.backend.command_extras}
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
        self._loop = asyncio.get_running_loop()
        self.application.create_background_task(self._ticker())
        if not self.backend.demo:
            self.refresh_readiness()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            if self.reduced_motion:
                continue
            if self.busy:
                self.tick()

    async def run_async(self) -> None:
        try:
            await self.application.run_async(pre_run=self._start_ticker, handle_sigint=False)
        finally:
            self._closed = True

    def run(self) -> None:
        try:
            self.application.run(pre_run=self._start_ticker)
        finally:
            self._closed = True


class DemoUI(TuiApp):
    """The prototype: fake backend plus the scripted starting states."""

    def __init__(self, state: str = "chat", **kw: Any) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; choose from {', '.join(STATES)}")
        self.state_name = state
        super().__init__(FakeBackend(empty=state.startswith("welcome"), blocked=state == "welcome-blocked"), **kw)

    def _seed(self) -> None:
        state = self.state_name
        self.add_message(Message("system", self.backend.intro_notice))
        if state in ("chat", "indexing", "sources", "evidence", "settings"):
            self.add_message(Message("user", fd.initial_question()))
            a = fd.initial_answer()
            self.add_message(Message("assistant", a.text, answer=a, mode_label=a.mode_label, elapsed=a.elapsed, scope_label=a.scope_label))
            self.last_question = fd.initial_question()
            self.focus_answer = 0
        if state == "welcome":
            self.overlays.append(ov.WelcomeOverlay(self))
        elif state == "welcome-blocked":
            self.overlays.append(ov.WelcomeOverlay(self))
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
