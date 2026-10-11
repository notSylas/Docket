"""CP7 in the full-screen UI: Browse (stub picker, never a real dialog),
a pasted or dragged folder opens Add instead of asking a question, and the
empty library suggests folders. Stub backends only; bounded waits."""

from __future__ import annotations

import threading
from pathlib import Path

from test_tui_folder_first import FolderStub, small_plan
from test_tui_real import scenario

from docket.interfaces.cli.picker import PickResult
from docket.interfaces.cli.tui import overlays as ov
from docket.services.sources.suggest import FolderSuggestion


class StubPicker:
    def __init__(self, result=None, ok=True):
        self.ok = ok
        self.result = result or PickResult(cancelled=True)
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.starts: list[str | None] = []

    def available(self):
        return self.ok

    def pick(self, start=None):
        self.starts.append(start)
        self.started.set()
        self.gate.wait(5)
        return self.result


class AddStub(FolderStub):
    def __init__(self, picker=None, suggestions=(), **kw):
        super().__init__(**kw)
        self.picker = picker
        self.suggestions = list(suggestions)
        self.added: list[str] = []
        self.checked: list[str] = []

    def check_folder(self, path):
        self.checked.append(path)
        return super().check_folder(path)

    def add_source(self, path):
        self.added.append(path)
        return super().add_source(path)

    def discover_folders(self):
        return list(self.suggestions)


async def _paste(h, text):
    """A terminal drag-and-drop arrives as a bracketed paste."""
    await h.send("\x1b[200~" + text + "\x1b[201~")


def _button_labels(h):
    return [b.label for b in h.ui.overlays[-1].buttons()]


def test_browse_hidden_without_picker_or_when_unavailable():
    async def go(h, b):
        h.ui.push(ov.AddFolderOverlay(h.ui))
        assert "Browse..." not in _button_labels(h)
        assert "Browse..." not in h.text()

    scenario(go, AddStub(picker=None))
    scenario(go, AddStub(picker=StubPicker(ok=False)))


def test_browse_visible_and_fills_the_path(tmp_path):
    chosen = tmp_path / "Reports"
    chosen.mkdir()
    picker = StubPicker(PickResult(path=str(chosen)))

    async def go(h, b):
        h.ui.push(ov.AddFolderOverlay(h.ui))
        assert "Browse..." in _button_labels(h)
        h.ui.overlays[-1].press("browse")
        await h.wait_for(lambda: b.added)
        assert b.checked == [str(chosen)] and b.added == [str(chosen)]
        assert picker.starts and picker.starts[0] is not None
        assert not any(o.name == "add" for o in h.ui.overlays)  # the flow went on

    scenario(go, AddStub(picker=picker))


def test_browse_waits_without_blocking_and_esc_stops_waiting(tmp_path):
    picker = StubPicker(PickResult(path=str(tmp_path)))
    picker.gate.clear()

    async def go(h, b):
        h.ui.push(ov.AddFolderOverlay(h.ui))
        h.ui.overlays[-1].press("browse")
        await h.wait_for(lambda: picker.started.is_set())
        await h.settle()
        assert "Waiting for the folder dialog..." in h.text()
        assert not b.added
        await h.type("x")  # the UI is still live
        await h.press("esc")
        assert h.ui.overlays and h.ui.overlays[-1].name == "add"
        assert "Waiting for the folder dialog" not in h.text()
        picker.gate.set()  # the user closes the dialog later: ignored
        await h.settle()
        await h.settle()
        assert b.added == []
        await h.press("esc")
        assert not h.ui.overlays or h.ui.overlays[-1].name != "add"

    scenario(go, AddStub(picker=picker))


def test_browse_cancel_and_error_leave_the_typed_path_usable():
    for res, word in ((PickResult(cancelled=True), "No folder was chosen"), (PickResult(error="boom"), "boom")):
        picker = StubPicker(res)

        async def go(h, b):
            h.ui.push(ov.AddFolderOverlay(h.ui))
            h.ui.overlays[-1].press("browse")
            await h.wait_for(lambda: word in h.text())
            assert h.ui.overlays[-1].name == "add" and not b.added

        scenario(go, AddStub(picker=picker))


def test_pasted_folder_opens_add_instead_of_asking(tmp_path):
    d = tmp_path / "My Docs"
    d.mkdir()

    async def go(h, b):
        await _paste(h, str(d).replace(" ", "\\ "))
        await h.press("enter")
        await h.wait_for(lambda: b.added)
        assert b.added == [str(d)]
        assert b.ask_calls == []
        assert h.ui.composer.text == ""

    scenario(go, AddStub())


def test_dragged_file_offers_its_folder(tmp_path):
    f = tmp_path / "plan.pdf"
    f.write_bytes(b"%PDF-1.4")

    async def go(h, b):
        await _paste(h, "'" + str(f) + "'")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.overlays and h.ui.overlays[-1].name == "confirm")
        assert "Docket works with folders - use its folder?" in h.text()
        assert str(tmp_path) in h.text()
        assert b.ask_calls == [] and not b.added
        await h.press("enter")  # Use this folder
        await h.wait_for(lambda: b.added)
        assert b.added == [str(tmp_path)]

    scenario(go, AddStub())


def test_a_sentence_with_a_path_is_a_question(tmp_path):
    async def go(h, b):
        await _paste(h, f"what is in {tmp_path}?")
        await h.press("enter")
        await h.wait_for(lambda: b.ask_calls)
        assert not b.added

    scenario(go, AddStub())


def test_slash_commands_still_win(tmp_path):
    async def go(h, b):
        await _paste(h, "/sources")
        await h.press("enter")
        await h.wait_for(lambda: h.ui.overlays and h.ui.overlays[-1].name == "sources")
        assert not b.added

    scenario(go, AddStub())


def test_empty_library_suggests_folders_and_enter_runs_the_flow():
    found = [
        FolderSuggestion("/work/Invoices", "Invoices", 12, {".pdf": 9, ".docx": 3}),
        FolderSuggestion("/work/Notes", "Notes", 3, {".docx": 3}),
    ]
    b = AddStub(suggestions=found, sources=False)
    b.next_plan = small_plan(12, "work/Invoices")
    b.consent = True

    async def go(h, b):
        await h.wait_for(lambda: h.ui.suggested_folders)
        await h.settle()
        text = h.text()
        assert "Folders that look like they hold documents" in text
        assert "Invoices" in text and "12 documents (9 PDF, 3 Word)" in text
        assert text.index("Invoices") < text.index("Notes")
        await h.press("enter")
        await h.wait_for(lambda: b.started)
        assert b.planned == ["/work/Invoices"]

    scenario(go, b)


def test_demo_backend_has_no_picker_or_suggestions():
    from docket.interfaces.cli.tui.headless import Harness, run

    async def go():
        async with Harness("chat", 100, 30) as h:
            assert h.ui.picker is None and not h.ui.picker_available()
            assert h.ui.suggested_folders == ()

    run(go, timeout=20)
