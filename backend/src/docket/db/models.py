"""SQLAlchemy 2.0 ORM models for the Docket "Evidence Core" schema.

This is a deliberately trimmed-down subset of the full DB design doc:
workspaces, authorized sources, sources, evidence versions/units, chunk
recipes, chunks, and ingestion jobs. Trust/sensitivity/multi-user tables are
out of scope for this checkpoint.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from docket.db.identity import compute_chunk_id, compute_recipe_id

__all__ = [
    "Base",
    "SourceStatus",
    "IngestionJobStatus",
    "Workspace",
    "AuthorizedSource",
    "Source",
    "EvidenceVersion",
    "EvidenceUnit",
    "ChunkRecipe",
    "Chunk",
    "IngestionJob",
    "compute_recipe_id",
    "compute_chunk_id",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SourceStatus(str, enum.Enum):
    ACTIVE = "active"
    MISSING = "missing"
    REVOKED = "revoked"
    TOMBSTONED = "tombstoned"
    HARD_DELETE_PENDING = "hard_delete_pending"
    DELETED = "deleted"


class IngestionJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _new_id("ws"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    authorized_sources: Mapped[list["AuthorizedSource"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    sources: Mapped[list["Source"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class AuthorizedSource(Base):
    __tablename__ = "authorized_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _new_id("auth"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False, index=True
    )
    scope_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    workspace: Mapped["Workspace"] = relationship(back_populates="authorized_sources")
    sources: Mapped[list["Source"]] = relationship(back_populates="authorized_source")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _new_id("src"))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False, index=True
    )
    authorized_source_id: Mapped[str] = mapped_column(
        ForeignKey("authorized_sources.id"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, name="source_status"),
        nullable=False,
        default=SourceStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=_utcnow, onupdate=_utcnow, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="sources")
    authorized_source: Mapped["AuthorizedSource"] = relationship(back_populates="sources")
    evidence_versions: Mapped[list["EvidenceVersion"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    ingestion_jobs: Mapped[list["IngestionJob"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class EvidenceVersion(Base):
    __tablename__ = "evidence_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _new_id("ev"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    # Which file within the source this version is for -- nullable for
    # backward compatibility with pre-CP8 rows/tests that only ever tracked
    # one file per source (see migration 0002's docstring for why this
    # exists: without it, "current version" can't be scoped per file for a
    # multi-file "local_folder" source).
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    parser_name: Mapped[str] = mapped_column(String, nullable=False)
    parser_version: Mapped[str] = mapped_column(String, nullable=False)
    formula_regions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_current: Mapped[bool] = mapped_column(default=True, nullable=False)

    source: Mapped["Source"] = relationship(back_populates="evidence_versions")
    evidence_units: Mapped[list["EvidenceUnit"]] = relationship(
        back_populates="evidence_version", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="evidence_version", cascade="all, delete-orphan"
    )


class EvidenceUnit(Base):
    __tablename__ = "evidence_units"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _new_id("eu"))
    evidence_version_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_versions.id"), nullable=False, index=True
    )
    unit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)

    evidence_version: Mapped["EvidenceVersion"] = relationship(back_populates="evidence_units")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="evidence_unit")


class ChunkRecipe(Base):
    """A chunking configuration. ``id`` is the content-derived recipe id
    (see :func:`docket.db.identity.compute_recipe_id`); there is no separate
    surrogate key.
    """

    __tablename__ = "chunk_recipes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    overlap: Mapped[int] = mapped_column(Integer, nullable=False)
    splitter: Mapped[str] = mapped_column(String, nullable=False)
    parser_name: Mapped[str] = mapped_column(String, nullable=False)
    parser_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="chunk_recipe")


class Chunk(Base):
    """A single chunk of text. ``id`` is the content-derived chunk id (see
    :func:`docket.db.identity.compute_chunk_id`); there is no separate
    surrogate key. Re-ingesting identical content with the same recipe
    produces the same id, so the primary key doubles as the dedup guard.
    """

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id"), nullable=False, index=True
    )
    evidence_version_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_versions.id"), nullable=False, index=True
    )
    evidence_unit_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_units.id"), nullable=False, index=True
    )
    chunk_recipe_id: Mapped[str] = mapped_column(
        ForeignKey("chunk_recipes.id"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 1-indexed page numbers this chunk spans (inclusive), per-chunk because
    # a chunk's sliding window can cross a page boundary that its owning
    # EvidenceUnit doesn't. Nullable: existing rows (ingested before this
    # column existed) and any chunk for which no page marker was ever
    # matched stay NULL rather than guessing -- see
    # docket.parsing.chunker's module docstring.
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped["Source"] = relationship(back_populates="chunks")
    evidence_version: Mapped["EvidenceVersion"] = relationship(back_populates="chunks")
    evidence_unit: Mapped["EvidenceUnit"] = relationship(back_populates="chunks")
    chunk_recipe: Mapped["ChunkRecipe"] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_source_id_content_hash", "source_id", "content_hash"),
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _new_id("job"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    status: Mapped[IngestionJobStatus] = mapped_column(
        Enum(IngestionJobStatus, name="ingestion_job_status"),
        nullable=False,
        default=IngestionJobStatus.PENDING,
    )
    started_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped["Source"] = relationship(back_populates="ingestion_jobs")
