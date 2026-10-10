"""Folder-first start in the full-screen UI: covered folder, one-time consent,
preview with subfolder checkboxes, empty folder, library suggestions.

A stub backend (fake data, in-memory consent, recorded calls); no services, no
model, no user data. Every scenario runs under a hard timeout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from test_tui_real import StubBackend, scenario

from docket.interfaces.cli.tui import fake_data as fd
from docket.interfaces.cli.tui.model import Scope
from docket.services.sources.scan import ScanResult
from docket.services.sources.session import (
    KIND_COVERED,
    KIND_EMPTY,
    KIND_LIBRARY,
    KIND_NEW,
    SessionPlan,
)


class FolderStub(StubBackend):
    def __init__(self, *, consent: bool = False, sources: bool = True) -> None:
        super().__init__(sources=sources)
        self.consent = consent
        self.consent_recorded = 0
        self.started: list[tuple[SessionPlan, tuple[str, ...]]] = []
        self.planned: list[str] = []
        self.next_plan: SessionPlan | None = None

    def consent_given(self) -> bool:
        return self.consent

    def record_consent(self) -> None:
        self.consent = True
        self.consent_recorded += 1

    def plan_for(self, folder):
        self.planned.append(str(folder))
        return self.next_plan or SessionPlan(KIND_LIBRARY, reason="test")

    def start_folder(self, plan, excluded=()):
        self.started.append((plan, tuple(excluded)))
        src = self.world.new_source_from_path(str(plan.folder))
        src.status, src.files = fd.READY, []
        return src


def small_plan(n: int = 4, name: str = "ForgeOPs/docs") -> SessionPlan:
    scan = ScanResult(
        root=Path("/work") / name,
        documents=n,
        by_type={".pdf": n},
        root_documents=n,
        estimated_seconds=4.0 * n,
    )
    return SessionPlan(KIND_NEW, folder=Path("/work") / name, name=name, scan=scan)


def broad_plan() -> SessionPlan:
    scan = ScanResult(
        root=Path("/work/archive"),
        documents=30,
        by_type={".pdf": 20, ".docx": 10},
        by_subfolder={"invoices": 20, "notes": 6, "misc": 2},
        root_documents=2,
        skipped={"ignored_folders": 3, "invalid_files": 1},
        total_bytes=5 * 1024 * 1024,
        estimated_seconds=600.0,
        broad=True,
        broad_reason="test",
    )
    return SessionPlan(
        KIND_NEW, folder=Path("/work/archive"), name="work/archive", scan=scan, needs_preview=True
    )


def test_covered_folder_scopes_chat_without_indexing():
    b = FolderStub()
    src = b.world.sources[0]
    plan = SessionPlan(
        KIND_COVERED,
        folder=Path(src.path),
        name="x",
        source=SimpleNamespace(id=src.id),
        source_name=src.name,
        relative_path="",
    )

    async def go(h, b):
        assert h.ui.scope == Scope("source", src.id)
        assert h.ui.job is None and b.index_calls == 0 and not h.ui.overlays
        assert src.name in h.screen_lines()[0]  # the header names the folder
        assert h.focused == "composer"

    scenario(go, b, session_plan=plan)


def test_covered_subfolder_scope_and_header():
    b = FolderStub()
    src = b.world.sources[0]
    plan = SessionPlan(
        KIND_COVERED,
        folder=Path(src.path) / "q1",
        name="x",
        source=SimpleNamespace(id=src.id),
        source_name=src.name,
        relative_path="q1/q2",
    )

    async def go(h, b):
        assert h.ui.scope == Scope("folder", src.id, "q1/q2")
        assert "q1/q2/" in h.screen_lines()[0]
        assert b.index_calls == 0

    scenario(go, b, session_plan=plan)


def test_new_small_folder_asks_once_then_indexes_in_the_background():
    b = FolderStub(consent=False, sources=False)
    b.index_gate.clear()

    async def go(h, b):
        text = h.text()
        assert "Read this folder?" in text
        assert "Docket will read 4 documents in ForgeOPs/docs" in text
        assert b.started == [] and b.consent_recorded == 0
        await h.press("enter")  # one key
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.total == 3)
        assert b.consent_recorded == 1 and b.consent
        plan, excluded = b.started[0]
        assert plan.name == "ForgeOPs/docs" and excluded == ()
        assert h.ui.job.active and h.focused == "composer"  # no modal, composer usable
        assert not h.ui.overlays
        assert h.ui.scope.kind == "source"
        assert "Reading 4 documents in ForgeOPs/docs" in h.text()
        await h.type("x")
        await h.press("enter")  # a question is accepted while indexing (CP4)
        await h.wait_for(lambda: len(b.ask_calls) == 1)
        assert "Sending is unavailable" not in h.text()
        assert b.ask_calls[0].index_coverage == {"indexed": 0, "total": 3, "indexing": True}
        b.index_gate.set()
        await h.wait_for(lambda: not h.ui.job.active)

    scenario(go, b, session_plan=small_plan(4))


def test_consent_given_before_starts_without_asking():
    b = FolderStub(consent=True, sources=False)
    b.index_gate.clear()

    async def go(h, b):
        await h.wait_for(lambda: h.ui.job is not None and h.ui.job.active)
        assert "Read this folder?" not in h.text()
        assert len(b.started) == 1 and b.consent_recorded == 0
        b.index_gate.set()
        await h.wait_for(lambda: not h.ui.job.active)

    scenario(go, b, session_plan=small_plan(2))


def test_declining_consent_indexes_nothing_and_shows_the_welcome():
    b = FolderStub(consent=False, sources=False)

    async def go(h, b):
        assert "Read this folder?" in h.text()
        await h.press("esc")
        assert b.started == [] and b.index_calls == 0 and not b.consent
        assert "Welcome to Docket" in h.text()

    scenario(go, b, session_plan=small_plan(4))


def test_consent_hot_keys():
    b = FolderStub(consent=False, sources=False)

    async def go(h, b):
        await h.type("n")
        assert b.started == [] and "Welcome to Docket" in h.text()

    scenario(go, b, session_plan=small_plan(4))

    b2 = FolderStub(consent=False, sources=False)
    b2.index_gate.clear()

    async def go2(h, b):
        await h.type("y")
        await h.wait_for(lambda: len(b.started) == 1)
        b.index_gate.set()
        await h.wait_for(lambda: not h.ui.job.active)

    scenario(go2, b2, session_plan=small_plan(4))


def test_preview_lists_counts_and_saves_unticked_subfolders():
    b = FolderStub(consent=True, sources=False)
    b.index_gate.clear()

    async def go(h, b):
        text = h.text()
        assert "Choose what to read" in text
        assert "work/archive holds 30 documents" in text
        assert "20 PDF" in text and "10 Word" in text
        assert "[x] invoices/" in text and "20 documents" in text
        assert "3 folders Docket never reads" in text and "1 file that are not real" in text
        assert "Plus 2 documents directly in this folder" in text
        assert "Index 30 documents" in text
        assert b.started == []  # preview shows even though consent exists: it is broad
        await h.type(" ")  # untick the first (largest) subfolder: invoices
        text = h.text()
        assert "[ ] invoices/" in text and "Index 10 documents" in text
        await h.type("i")
        await h.wait_for(lambda: len(b.started) == 1)
        plan, excluded = b.started[0]
        assert excluded == ("invoices",)
        assert h.ui.job.active and h.focused == "composer"
        b.index_gate.set()
        await h.wait_for(lambda: not h.ui.job.active)

    scenario(go, b, session_plan=broad_plan())


def test_preview_with_everything_unticked_cannot_start():
    b = FolderStub(consent=True, sources=False)
    plan = broad_plan()
    plan = SessionPlan(
        plan.kind,
        folder=plan.folder,
        name=plan.name,
        needs_preview=True,
        scan=ScanResult(**{**plan.scan.__dict__, "root_documents": 0, "documents": 28}),
    )

    async def go(h, b):
        for _ in range(3):
            await h.type(" ")
            await h.press("down")
        assert "Index 0 documents" in h.text()
        await h.type("i")
        assert "Tick at least one folder first." in h.text()
        assert b.started == []

    scenario(go, b, session_plan=plan)


def test_preview_root_documents_always_count():
    b = FolderStub(consent=True, sources=False)

    async def go(h, b):
        for _ in range(3):
            await h.type(" ")
            await h.press("down")
        assert "Index 2 documents" in h.text()

    scenario(go, b, session_plan=broad_plan())


def test_preview_cancel_and_choose_another():
    b = FolderStub(consent=True, sources=False)

    async def go(h, b):
        await h.type("c")
        assert "Add folder" in h.text()
        assert b.started == []

    scenario(go, b, session_plan=broad_plan())

    b2 = FolderStub(consent=True, sources=False)

    async def go2(h, b):
        await h.type("n")
        assert b.started == [] and "Welcome to Docket" in h.text()

    scenario(go2, b2, session_plan=broad_plan())


def test_empty_folder_explains_and_offers_the_library():
    b = FolderStub(consent=True)
    plan = SessionPlan(
        KIND_EMPTY, folder=Path("/work/none"), name="work/none", reason="no supported documents"
    )

    async def go(h, b):
        text = h.text()
        assert "Nothing to read here" in text
        assert "nothing to read in work/none: no supported documents" in text
        assert b.started == [] and b.index_calls == 0
        await h.press("enter")  # Open the library
        assert "Sources" in h.text()

    scenario(go, b, session_plan=plan)


def test_library_with_suggestions_starts_a_folder_on_enter():
    b = FolderStub(consent=False, sources=False)
    b.next_plan = small_plan(3, "Downloads/taxes")
    plan = SessionPlan(
        KIND_LIBRARY,
        folder=Path("/home/u"),
        reason="your home folder",
        suggestions=(("/home/u/Downloads/taxes", 3), ("/home/u/Documents", 1)),
    )

    async def go(h, b):
        text = h.text()
        assert "Folders here that hold documents" in text
        assert "taxes" in text and "3 documents" in text
        await h.press("enter")  # first suggestion
        assert b.planned == ["/home/u/Downloads/taxes"]
        assert "Read this folder?" in h.text()
        assert "3 documents in Downloads/taxes" in h.text()

    scenario(go, b, session_plan=plan)


def test_library_without_suggestions_is_the_old_welcome():
    b = FolderStub(sources=False)

    async def go(h, b):
        assert "Welcome to Docket" in h.text()
        assert "Folders here that hold documents" not in h.text()

    scenario(go, b, session_plan=SessionPlan(KIND_LIBRARY, reason="no folder"))


def test_library_with_sources_opens_plain_chat():
    b = FolderStub()

    async def go(h, b):
        assert not h.ui.overlays and h.ui.scope == Scope()

    scenario(go, b, session_plan=SessionPlan(KIND_LIBRARY, reason="your home folder"))


def test_add_folder_overlay_shows_a_preview_for_a_broad_folder():
    b = FolderStub(consent=True, sources=False)
    b.next_plan = broad_plan()

    async def go(h, b):
        await h.press("enter")  # Welcome: Add a folder
        await h.type("/work/archive")
        await h.press("enter")
        assert "Choose what to read" in h.text()
        assert b.planned == ["/work/archive"]

    scenario(go, b)


# -- the real backend over temp trees and a fake pipeline ------------------------------


def _real_tree(tmp_path: Path) -> Path:
    root = tmp_path / "ForgeOPs" / "docs"
    for name in ("a.pdf", "guides/b.pdf", "guides/c.pdf", "old/d.pdf"):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4\n" + b"x" * 32)
    return root


def test_real_backend_first_launch_consent_index_then_covered_relaunch(tmp_path):
    import json

    from test_tui_real import backend_for, make_context

    root = _real_tree(tmp_path)
    ctx = make_context(tmp_path, [(root / "a.pdf", "ingested", None), (root / "guides/b.pdf", "ingested", None)])
    be = backend_for(ctx)
    plan = be.plan_for(root)
    assert plan.kind == KIND_NEW and plan.documents == 4 and not plan.needs_preview
    assert not be.consent_given()

    async def first(h, _b):
        assert "Docket will read 4 documents in ForgeOPs/docs" in h.text()
        await h.press("enter")
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)
        assert h.ui.job.state == "done"

    scenario(first, be, session_plan=plan)

    sources = ctx.source_manager.list_sources()
    assert len(sources) == 1 and sources[0].display_name == "docs"
    assert be.consent_given()
    assert "folder_consent_v1" in json.loads((ctx.settings.data_dir / "preferences.json").read_text())
    pipeline = ctx.pipeline
    assert pipeline.runs == [sources[0].id]

    # second launch, from a subfolder of the now-registered source: reuse, no indexing
    again = be.plan_for(root / "guides")
    assert again.kind == KIND_COVERED and again.relative_path == "guides"

    async def second(h, _b):
        assert h.ui.scope == Scope("folder", sources[0].id, "guides")
        assert h.ui.job is None and not h.ui.overlays
        assert "docs / guides/" in h.screen_lines()[0]

    scenario(second, be, session_plan=again)
    assert pipeline.runs == [sources[0].id]  # nothing re-indexed
    assert len(ctx.source_manager.list_sources()) == 1


def test_real_backend_preview_saves_exclude_rules_and_discovery_honours_them(tmp_path):
    import json

    from test_tui_real import backend_for, make_context

    from docket.services.ingestion.ignore import discover

    root = _real_tree(tmp_path)
    ctx = make_context(tmp_path, [(root / "a.pdf", "ingested", None)])
    be = backend_for(ctx)
    be.record_consent()
    plan = be.plan_for(root)
    # force a preview, as a broad or large folder would
    plan = SessionPlan(plan.kind, folder=plan.folder, name=plan.name, scan=plan.scan, needs_preview=True)

    async def go(h, _b):
        text = h.text()
        assert "[x] guides/" in text and "[x] old/" in text
        await h.type(" ")  # guides is first (2 documents): untick it
        await h.type("i")
        await h.wait_for(lambda: h.ui.job is not None and not h.ui.job.active)

    scenario(go, be, session_plan=plan)

    src = ctx.source_manager.list_sources()[0]
    assert json.loads(src.rules_json)["exclude"] == ["/guides/"]
    kept = discover(root, extensions={".pdf"}, extra_rules=src.rules_json).files
    assert sorted(p.relative_to(root).as_posix() for p in kept) == ["a.pdf", "old/d.pdf"]
