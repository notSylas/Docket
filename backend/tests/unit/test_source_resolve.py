"""`services/sources/resolve.py` (names, numbers, prefixes) and the
no-overlap registration rule in `SourceManager.register_source`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import Base, SourceStatus
from docket.services.ingestion.ignore import IgnoreRules
from docket.services.sources.manager import SourceCoveredError, SourceManager
from docket.services.sources.resolve import (
    AmbiguousSourceRefError,
    UnknownSourceRefError,
    display_names,
    ordered_sources,
    resolve_source,
)

T0 = datetime(2026, 1, 1)


def _src(sid: str, path: str, n: int = 0, display_name: str | None = None, status=SourceStatus.ACTIVE):
    return SimpleNamespace(
        id=sid,
        path=path,
        display_name=display_name,
        status=status,
        created_at=T0 + timedelta(minutes=n),
    )


# -- display names ---------------------------------------------------------


def test_names_use_folder_basename_and_stored_name():
    a = _src("src_a", "/home/me/ForgeOPs/docs", 0)
    b = _src("src_b", "/home/me/Finance", 1, display_name="Money")
    assert display_names([a, b]) == {"src_a": "docs", "src_b": "Money"}


def test_collisions_add_parent_then_longer_suffix():
    a = _src("src_a", "/home/me/ForgeOPs/docs", 0)
    b = _src("src_b", "/home/me/Other/docs", 1)
    names = display_names([a, b])
    assert names == {"src_a": "docs (ForgeOPs)", "src_b": "docs (Other)"}

    c = _src("src_c", "/w1/proj/docs", 0)
    d = _src("src_d", "/w2/proj/docs", 1)
    names = display_names([c, d])
    assert names == {"src_c": "docs (w1/proj)", "src_d": "docs (w2/proj)"}


def test_unique_names_are_untouched_by_other_collisions():
    a = _src("src_a", "/x/docs", 0)
    b = _src("src_b", "/y/docs", 1)
    c = _src("src_c", "/z/notes", 2)
    assert display_names([a, b, c])["src_c"] == "notes"


# -- resolve ---------------------------------------------------------------


@pytest.fixture()
def sources():
    return [
        _src("src_1111aaaa", "/home/me/ForgeOPs/docs", 0),
        _src("src_2222bbbb", "/home/me/Other/docs", 1),
        _src("src_3333cccc", "/home/me/Finance", 2),
    ]


def test_resolve_by_number_matches_list_order(sources):
    shuffled = [sources[2], sources[0], sources[1]]
    assert [s.id for s in ordered_sources(shuffled)] == [s.id for s in sources]
    assert resolve_source("1", shuffled).id == "src_1111aaaa"
    assert resolve_source("3", shuffled).id == "src_3333cccc"


def test_resolve_number_out_of_range(sources):
    with pytest.raises(UnknownSourceRefError, match="1-3"):
        resolve_source("9", sources)


def test_resolve_by_name_is_case_insensitive(sources):
    assert resolve_source("finance", sources).id == "src_3333cccc"
    assert resolve_source("DOCS (forgeops)", sources).id == "src_1111aaaa"


def test_ambiguous_bare_name_lists_candidates(sources):
    with pytest.raises(AmbiguousSourceRefError) as info:
        resolve_source("docs", sources)
    message = str(info.value)
    assert "src_1111aaaa" in message and "src_2222bbbb" in message
    assert "/home/me/ForgeOPs/docs" in message
    assert len(info.value.candidates) == 2


def test_resolve_by_prefix_and_full_id(sources):
    assert resolve_source("src_33", sources).id == "src_3333cccc"
    assert resolve_source("src_2222bbbb", sources).id == "src_2222bbbb"
    with pytest.raises(AmbiguousSourceRefError, match="src_1111aaaa"):
        resolve_source("src_", sources)


def test_unknown_lists_known_sources(sources):
    with pytest.raises(UnknownSourceRefError) as info:
        resolve_source("nope", sources)
    assert "source not found: nope" in str(info.value)
    assert "Finance" in str(info.value)


def test_deleted_source_does_not_block_a_live_name():
    old = _src("src_old", "/a/docs", 0, status=SourceStatus.DELETED)
    live = _src("src_live", "/b/docs", 1)
    # Names collide so labels carry the parent; the bare name still resolves
    # to the live one only.
    assert resolve_source("docs", [old, live]).id == "src_live"


def test_resolve_with_no_sources():
    with pytest.raises(UnknownSourceRefError):
        resolve_source("x", [])


# -- manager: overlap rule ---------------------------------------------------


@pytest.fixture()
def manager(tmp_path: Path):
    engine = get_engine(tmp_path / "docket.sqlite3")
    Base.metadata.create_all(engine)
    yield SourceManager(get_session_factory(engine))
    engine.dispose()


def test_display_name_is_stored_and_blank_is_none(manager, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    assert manager.register_source(a, display_name="  Alpha ").display_name == "Alpha"
    assert manager.register_source(b, display_name="  ").display_name is None


def test_register_inside_active_source_raises_covered(manager, tmp_path):
    parent = tmp_path / "work"
    (parent / "sub").mkdir(parents=True)
    existing = manager.register_source(parent, display_name="Work")
    with pytest.raises(SourceCoveredError) as info:
        manager.register_source(parent / "sub")
    assert info.value.covering.id == existing.id
    assert "already covered by Work" in str(info.value)
    assert len(manager.list_sources()) == 1


def test_register_inside_revoked_source_is_allowed(manager, tmp_path):
    parent = tmp_path / "work"
    (parent / "sub").mkdir(parents=True)
    existing = manager.register_source(parent)
    manager.deactivate_source(existing.id, reason="user_disconnected")
    assert manager.register_source(parent / "sub").path == str(parent / "sub")


def test_register_parent_stores_child_excludes(manager, tmp_path):
    root = tmp_path / "desk"
    (root / "a" / "b").mkdir(parents=True)
    (root / "c").mkdir()
    child1 = manager.register_source(root / "a" / "b")
    child2 = manager.register_source(root / "c")
    parent = manager.register_source(root)

    rules = json.loads(parent.rules_json)
    assert rules["version"] == 1
    assert rules["exclude"] == ["a/b/", "c/"]
    # Children remain sources and untouched.
    ids = {s.id for s in manager.list_sources()}
    assert {child1.id, child2.id, parent.id} == ids
    assert manager.get_source(child1.id).rules_json is None

    # The stored rules really exclude those folders in discovery.
    ignore = IgnoreRules.for_root(root, parent.rules_json)
    assert ignore.path_ignored(Path("a/b/x.pdf"))
    assert ignore.path_ignored(Path("c/x.pdf"))
    assert not ignore.path_ignored(Path("a/x.pdf"))


def test_register_parent_ignores_disconnected_children(manager, tmp_path):
    root = tmp_path / "desk"
    (root / "a").mkdir(parents=True)
    child = manager.register_source(root / "a")
    manager.deactivate_source(child.id, reason="user_disconnected")
    assert manager.register_source(root).rules_json is None


def test_exact_duplicate_registers_as_before(manager, tmp_path):
    f = tmp_path / "f"
    f.mkdir()
    first = manager.register_source(f)
    second = manager.register_source(f)
    assert first.id != second.id


def test_get_source_by_ref(manager, tmp_path):
    f = tmp_path / "Reports"
    f.mkdir()
    src = manager.register_source(f)
    assert manager.get_source_by_ref("reports").id == src.id
    assert manager.get_source_by_ref("1").id == src.id
    assert manager.get_source_by_ref(src.id[:6]).id == src.id
