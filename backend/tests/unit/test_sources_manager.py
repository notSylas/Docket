"""Tests for `attest.sources.manager.SourceManager`."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from attest.db.engine import get_engine, get_session_factory
from attest.db.models import Base, SourceStatus
from attest.sources.manager import SourceManager, SourceNotFoundError


@pytest.fixture()
def session_factory(tmp_path: Path) -> sessionmaker:
    engine = get_engine(tmp_path / "attest.sqlite3")
    Base.metadata.create_all(engine)
    factory = get_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def manager(session_factory: sessionmaker) -> SourceManager:
    return SourceManager(session_factory)


def test_get_or_create_default_workspace_is_idempotent(manager: SourceManager) -> None:
    first = manager.get_or_create_default_workspace()
    second = manager.get_or_create_default_workspace()

    assert first.id == second.id
    assert first.name == "default"


def test_register_source_creates_source_and_authorized_source(
    manager: SourceManager, tmp_path: Path
) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()

    source = manager.register_source(folder)

    assert source.source_type == "local_folder"
    assert source.path == str(folder)
    assert source.status == SourceStatus.ACTIVE
    assert source.workspace_id == manager.get_or_create_default_workspace().id
    assert source.authorized_source_id is not None


def test_register_source_nonexistent_path_raises_value_error(
    manager: SourceManager, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ValueError):
        manager.register_source(missing)


def test_register_source_file_not_directory_raises_value_error(
    manager: SourceManager, tmp_path: Path
) -> None:
    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("hello")

    with pytest.raises(ValueError):
        manager.register_source(a_file)


def test_list_sources_returns_registered_sources(manager: SourceManager, tmp_path: Path) -> None:
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()

    source_a = manager.register_source(folder_a)
    source_b = manager.register_source(folder_b)

    sources = manager.list_sources()
    ids = {s.id for s in sources}
    assert {source_a.id, source_b.id} <= ids


def test_list_sources_filters_by_workspace_id(manager: SourceManager, tmp_path: Path) -> None:
    folder = tmp_path / "a"
    folder.mkdir()
    source = manager.register_source(folder)

    other_workspace = manager.get_or_create_default_workspace()
    sources_for_workspace = manager.list_sources(workspace_id=other_workspace.id)
    assert source.id in {s.id for s in sources_for_workspace}

    sources_for_bogus_workspace = manager.list_sources(workspace_id="ws_bogus")
    assert sources_for_bogus_workspace == []


def test_get_source_returns_source(manager: SourceManager, tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    created = manager.register_source(folder)

    fetched = manager.get_source(created.id)
    assert fetched.id == created.id


def test_get_source_unknown_id_raises_source_not_found_error(manager: SourceManager) -> None:
    with pytest.raises(SourceNotFoundError):
        manager.get_source("src_does_not_exist")


def test_deactivate_source_sets_status_revoked(manager: SourceManager, tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    source = manager.register_source(folder)

    manager.deactivate_source(source.id)

    refreshed = manager.get_source(source.id)
    assert refreshed.status == SourceStatus.REVOKED


def test_deactivate_source_unknown_id_raises_source_not_found_error(
    manager: SourceManager,
) -> None:
    with pytest.raises(SourceNotFoundError):
        manager.deactivate_source("src_does_not_exist")
