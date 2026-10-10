"""Tests for `docket.services.sources.manager.SourceManager`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from docket.core.config import Settings
from docket.core.db.engine import get_engine, get_session_factory
from docket.core.db.models import Base, Source, SourceStatus
from docket.services.sources.manager import (
    InvalidSourceTransitionError,
    SourceManager,
    SourceNotFoundError,
)


@pytest.fixture()
def session_factory(tmp_path: Path) -> sessionmaker:
    engine = get_engine(tmp_path / "docket.sqlite3")
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


# ---------------------------------------------------------------------------
# SourceStatus state machine (Upgrade doc 03 section 6): the rest of the
# transition graph beyond ACTIVE->REVOKED.
# ---------------------------------------------------------------------------


def _register(manager: SourceManager, tmp_path: Path, name: str = "docs") -> Source:
    folder = tmp_path / name
    folder.mkdir()
    return manager.register_source(folder)


def _set_status(
    session_factory: sessionmaker,
    source_id: str,
    status: SourceStatus,
    *,
    retention_deadline: datetime | None = None,
) -> None:
    with session_factory() as session:
        source = session.get(Source, source_id)
        source.status = status
        if retention_deadline is not None:
            source.retention_deadline = retention_deadline
        session.add(source)
        session.commit()


# -- ACTIVE <-> MISSING ------------------------------------------------------


def test_mark_unreachable_transitions_active_to_missing(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)

    manager.mark_unreachable(source.id)

    assert manager.get_source(source.id).status == SourceStatus.MISSING


def test_mark_unreachable_is_idempotent_when_already_missing(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)

    manager.mark_unreachable(source.id)  # must not raise

    assert manager.get_source(source.id).status == SourceStatus.MISSING


def test_mark_unreachable_from_revoked_raises_invalid_transition(
    manager: SourceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id)

    with pytest.raises(InvalidSourceTransitionError):
        manager.mark_unreachable(source.id)


def test_mark_unreachable_unknown_id_raises_source_not_found(manager: SourceManager) -> None:
    with pytest.raises(SourceNotFoundError):
        manager.mark_unreachable("src_does_not_exist")


def test_mark_reachable_transitions_missing_to_active(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)

    manager.mark_reachable(source.id)

    assert manager.get_source(source.id).status == SourceStatus.ACTIVE


def test_mark_reachable_is_idempotent_when_already_active(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)

    manager.mark_reachable(source.id)  # already ACTIVE -- must not raise

    assert manager.get_source(source.id).status == SourceStatus.ACTIVE


def test_mark_reachable_from_revoked_raises_invalid_transition(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id)

    with pytest.raises(InvalidSourceTransitionError):
        manager.mark_reachable(source.id)


# -- MISSING -> TOMBSTONED ----------------------------------------------------


def test_mark_tombstoned_transitions_missing_to_tombstoned_and_sets_retention_deadline(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    before = datetime.now(timezone.utc)

    manager.mark_tombstoned(source.id, reason="confirmed_deleted_upstream")

    refreshed = manager.get_source(source.id)
    assert refreshed.status == SourceStatus.TOMBSTONED
    assert refreshed.status_reason == "confirmed_deleted_upstream"
    assert refreshed.retention_deadline is not None
    # Default is settings.tombstone_retention_days (30), within a tolerance
    # for test execution time.
    expected = before.replace(tzinfo=None) + timedelta(days=30)
    actual = refreshed.retention_deadline
    assert abs((actual - expected).total_seconds()) < 5


def test_mark_tombstoned_honors_explicit_retention_days(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    before = datetime.now(timezone.utc)

    manager.mark_tombstoned(source.id, retention_days=5)

    refreshed = manager.get_source(source.id)
    expected = before.replace(tzinfo=None) + timedelta(days=5)
    assert abs((refreshed.retention_deadline - expected).total_seconds()) < 5


def test_mark_tombstoned_from_active_raises_invalid_transition(
    manager: SourceManager, tmp_path: Path
) -> None:
    """TOMBSTONED is only reachable from MISSING -- never directly from
    ACTIVE, and never automatically from repeated mark_unreachable calls
    alone (Upgrade doc 03 section 6's explicit decision)."""
    source = _register(manager, tmp_path)

    with pytest.raises(InvalidSourceTransitionError):
        manager.mark_tombstoned(source.id)


def test_repeated_mark_unreachable_never_escalates_to_tombstoned(
    manager: SourceManager, tmp_path: Path
) -> None:
    """No amount of repeated `mark_unreachable` calls alone should ever
    drive a source to TOMBSTONED -- only an explicit `mark_tombstoned` call
    can (Upgrade doc 03 section 6's explicit decision)."""
    source = _register(manager, tmp_path)

    for _ in range(5):
        manager.mark_unreachable(source.id)

    assert manager.get_source(source.id).status == SourceStatus.MISSING


# -- ACTIVE/MISSING -> REVOKED (with reason) ---------------------------------


def test_deactivate_source_from_missing_sets_status_revoked(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)

    manager.deactivate_source(source.id, reason="access_revoked")

    refreshed = manager.get_source(source.id)
    assert refreshed.status == SourceStatus.REVOKED
    assert refreshed.status_reason == "access_revoked"


def test_deactivate_source_records_reason_for_manual_disconnect(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)

    manager.deactivate_source(source.id, reason="user_disconnected")

    assert manager.get_source(source.id).status_reason == "user_disconnected"


def test_deactivate_source_without_reason_leaves_status_reason_none(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)

    manager.deactivate_source(source.id)

    assert manager.get_source(source.id).status_reason is None


def test_deactivate_source_is_idempotent_when_already_revoked(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id, reason="user_disconnected")

    manager.deactivate_source(source.id, reason="access_revoked")  # must not raise

    refreshed = manager.get_source(source.id)
    assert refreshed.status == SourceStatus.REVOKED
    assert refreshed.status_reason == "access_revoked"


def test_deactivate_source_from_tombstoned_raises_invalid_transition(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    manager.mark_tombstoned(source.id)

    with pytest.raises(InvalidSourceTransitionError):
        manager.deactivate_source(source.id)


# -- REVOKED/TOMBSTONED -> HARD_DELETE_PENDING -------------------------------


def test_request_hard_delete_from_revoked(manager: SourceManager, tmp_path: Path) -> None:
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id)

    manager.request_hard_delete(source.id, reason="user_confirmed_delete")

    refreshed = manager.get_source(source.id)
    assert refreshed.status == SourceStatus.HARD_DELETE_PENDING
    assert refreshed.status_reason == "user_confirmed_delete"


def test_request_hard_delete_from_tombstoned(manager: SourceManager, tmp_path: Path) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    manager.mark_tombstoned(source.id)

    manager.request_hard_delete(source.id)

    assert manager.get_source(source.id).status == SourceStatus.HARD_DELETE_PENDING


def test_request_hard_delete_from_active_raises_invalid_transition(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)

    with pytest.raises(InvalidSourceTransitionError):
        manager.request_hard_delete(source.id)


# -- sweep_expired_retentions -------------------------------------------------


def test_sweep_expired_retentions_advances_expired_tombstoned_sources(
    manager: SourceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    manager.mark_tombstoned(source.id)
    past_deadline = datetime.now(timezone.utc) - timedelta(days=1)
    _set_status(
        session_factory, source.id, SourceStatus.TOMBSTONED, retention_deadline=past_deadline
    )

    advanced = manager.sweep_expired_retentions()

    assert advanced == [source.id]
    refreshed = manager.get_source(source.id)
    assert refreshed.status == SourceStatus.HARD_DELETE_PENDING
    assert refreshed.status_reason == "retention_deadline_expired"


def test_sweep_expired_retentions_leaves_unexpired_sources_alone(
    manager: SourceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    manager.mark_tombstoned(source.id)  # deadline 30 days out by default

    advanced = manager.sweep_expired_retentions()

    assert advanced == []
    assert manager.get_source(source.id).status == SourceStatus.TOMBSTONED


def test_sweep_expired_retentions_ignores_non_tombstoned_sources(
    manager: SourceManager, tmp_path: Path
) -> None:
    active = _register(manager, tmp_path, name="a")
    revoked = _register(manager, tmp_path, name="b")
    manager.deactivate_source(revoked.id)

    advanced = manager.sweep_expired_retentions()

    assert advanced == []
    assert manager.get_source(active.id).status == SourceStatus.ACTIVE
    assert manager.get_source(revoked.id).status == SourceStatus.REVOKED


def test_sweep_expired_retentions_respects_explicit_now(
    manager: SourceManager, session_factory: sessionmaker, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    manager.mark_tombstoned(source.id, retention_days=10)

    # 9 days later: not expired yet.
    still_pending = manager.sweep_expired_retentions(
        now=datetime.now(timezone.utc) + timedelta(days=9)
    )
    assert still_pending == []

    # 11 days later: expired.
    advanced = manager.sweep_expired_retentions(
        now=datetime.now(timezone.utc) + timedelta(days=11)
    )
    assert advanced == [source.id]


# -- pause/resume sync (orthogonal to SourceStatus) --------------------------


def test_pause_sync_sets_sync_paused_at_without_changing_status(
    manager: SourceManager, tmp_path: Path
) -> None:
    source = _register(manager, tmp_path)

    manager.pause_sync(source.id)

    refreshed = manager.get_source(source.id)
    assert refreshed.sync_paused_at is not None
    assert refreshed.status == SourceStatus.ACTIVE


def test_resume_sync_clears_sync_paused_at(manager: SourceManager, tmp_path: Path) -> None:
    source = _register(manager, tmp_path)
    manager.pause_sync(source.id)

    manager.resume_sync(source.id)

    assert manager.get_source(source.id).sync_paused_at is None


def test_resume_sync_is_a_noop_when_not_paused(manager: SourceManager, tmp_path: Path) -> None:
    source = _register(manager, tmp_path)

    manager.resume_sync(source.id)  # must not raise

    assert manager.get_source(source.id).sync_paused_at is None


def test_pause_sync_unknown_id_raises_source_not_found(manager: SourceManager) -> None:
    with pytest.raises(SourceNotFoundError):
        manager.pause_sync("src_does_not_exist")


# -- settings injection -------------------------------------------------------


def test_source_manager_honors_injected_settings_for_default_retention(
    session_factory: sessionmaker, tmp_path: Path
) -> None:
    custom_settings = Settings(data_dir=tmp_path, tombstone_retention_days=3)
    manager = SourceManager(session_factory, settings=custom_settings)
    source = _register(manager, tmp_path)
    manager.mark_unreachable(source.id)
    before = datetime.now(timezone.utc)

    manager.mark_tombstoned(source.id)

    refreshed = manager.get_source(source.id)
    expected = before.replace(tzinfo=None) + timedelta(days=3)
    assert abs((refreshed.retention_deadline - expected).total_seconds()) < 5


@pytest.mark.parametrize("reason", [None, "user_disconnected"])
def test_reconnect_retains_source_and_authorization(manager, tmp_path, reason):
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id, reason=reason)
    restored = manager.reconnect_source(source.id)
    assert restored.id == source.id
    assert restored.authorized_source_id == source.authorized_source_id
    assert restored.status == SourceStatus.ACTIVE
    assert restored.status_reason is None
    assert len(manager.list_sources()) == 1


def test_reconnect_requires_reachable_folder(manager, tmp_path):
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id)
    Path(source.path).rmdir()
    with pytest.raises(ValueError, match="unavailable"):
        manager.reconnect_source(source.id)
    assert manager.get_source(source.id).status == SourceStatus.REVOKED


def test_reconnect_refuses_connector_access_loss(manager, tmp_path):
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id, reason="access_revoked")
    with pytest.raises(ValueError, match="authorization"):
        manager.reconnect_source(source.id)
    assert manager.get_source(source.id).status == SourceStatus.REVOKED


@pytest.mark.parametrize("status", [SourceStatus.ACTIVE, SourceStatus.TOMBSTONED, SourceStatus.HARD_DELETE_PENDING, SourceStatus.DELETED])
def test_reconnect_does_not_resurrect_other_states(manager, session_factory, tmp_path, status):
    source = _register(manager, tmp_path)
    _set_status(session_factory, source.id, status)
    with pytest.raises(InvalidSourceTransitionError):
        manager.reconnect_source(source.id)
    assert manager.get_source(source.id).status == status


def test_reconnect_unknown_source(manager):
    with pytest.raises(SourceNotFoundError):
        manager.reconnect_source("does-not-exist")


def test_reconnect_rejects_nonlocal_source(manager, session_factory, tmp_path):
    source = _register(manager, tmp_path)
    manager.deactivate_source(source.id)
    with session_factory() as session:
        session.get(Source, source.id).source_type = "remote_connector"
        session.commit()
    with pytest.raises(ValueError, match="local"):
        manager.reconnect_source(source.id)
