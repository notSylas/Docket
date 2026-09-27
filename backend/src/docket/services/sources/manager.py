"""`SourceManager` -- registers/lists/looks up/deactivates ``Source`` rows.

Solo-use scope note: the approved plan defers multi-workspace support, so
this checkpoint works against a single implicit "default" workspace
(``get_or_create_default_workspace``) rather than requiring every caller to
juggle a workspace id. Callers that DO have a specific ``workspace_id`` (e.g.
a future multi-workspace UI) can still pass one explicitly to
``register_source``/``list_sources``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import AuthorizedSource, Source, SourceStatus, Workspace

DEFAULT_WORKSPACE_NAME = "default"


class SourceNotFoundError(Exception):
    def __init__(self, source_id: str):
        self.source_id = source_id
        super().__init__(f"source not found: {source_id}")


class SourceManager:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_or_create_default_workspace(self) -> Workspace:
        """Return the single default workspace, creating it if it doesn't
        exist yet. Idempotent: repeated calls return the same row."""
        with self._session_factory() as session:
            workspace = session.execute(
                select(Workspace).where(Workspace.name == DEFAULT_WORKSPACE_NAME)
            ).scalar_one_or_none()
            if workspace is not None:
                return workspace

            workspace = Workspace(name=DEFAULT_WORKSPACE_NAME)
            session.add(workspace)
            session.commit()
            session.refresh(workspace)
            return workspace

    def register_source(self, path: Path, *, workspace_id: str | None = None) -> Source:
        """Register a local folder as a source.

        Validates ``path`` exists and is a directory before touching the DB
        -- registering a bogus path would silently create a Source that can
        never successfully ingest anything, so we fail fast here instead.
        """
        path = Path(path)
        if not path.exists():
            raise ValueError(f"source path does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"source path is not a directory: {path}")

        if workspace_id is None:
            workspace_id = self.get_or_create_default_workspace().id

        with self._session_factory() as session:
            authorized_source = AuthorizedSource(
                workspace_id=workspace_id, scope_path=str(path)
            )
            session.add(authorized_source)
            session.flush()

            source = Source(
                workspace_id=workspace_id,
                authorized_source_id=authorized_source.id,
                source_type="local_folder",
                path=str(path),
                status=SourceStatus.ACTIVE,
            )
            session.add(source)
            session.commit()
            session.refresh(source)
            return source

    def list_sources(self, *, workspace_id: str | None = None) -> list[Source]:
        with self._session_factory() as session:
            stmt = select(Source)
            if workspace_id is not None:
                stmt = stmt.where(Source.workspace_id == workspace_id)
            return list(session.execute(stmt).scalars().all())

    def get_source(self, source_id: str) -> Source:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            return source

    def deactivate_source(self, source_id: str) -> None:
        """Flip ``status`` to ``REVOKED``. Does not delete any data -- actual
        data removal on revocation is a later Trust/Policy checkpoint's
        concern."""
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceNotFoundError(source_id)
            source.status = SourceStatus.REVOKED
            session.add(source)
            session.commit()
