"""Query scope: an enforced boundary on what one question may search and read
(Upgrade doc 17 section 5.4; doc 05 section 6 for the named-file scope it
composes with).

`QueryScope` is what a caller asks for: everything, a folder path, or a source.
`resolve` turns it, per question, into the eligible READY evidence-version ids
of ACTIVE sources under it, using the one eligibility query shared with
`signals.load_eligible_files`. The result is an OUTER boundary: a file named in
the question may narrow it, never widen it.

Two states must never be confused, which is why `ResolvedScope` is explicit:

- no scope (`None`, or `everything`): `version_ids is None`, nothing restricted;
- a scope that matches nothing: `version_ids == ()` and `empty is True`; the
  caller must not search at all (an empty list silently meaning "everything"
  was the bug this module exists to remove).

Folder containment is component-wise: `/a/docs` contains `/a/docs/x.pdf` and
`/a/docs` itself, never `/a/docs2/x.pdf`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy import Engine, text

from docket.core.db.models import SourceStatus
from docket.services.query.signals import load_eligible_versions
from docket.services.query.trust import AbstentionReason


class ScopeKind(StrEnum):
    EVERYTHING = "everything"
    FOLDER = "folder"
    SOURCE = "source"


@dataclass(frozen=True)
class QueryScope:
    kind: ScopeKind = ScopeKind.EVERYTHING
    folder: str | None = None  # kind == FOLDER: a directory (or a single file) path
    source_id: str | None = None  # kind == SOURCE
    label: str = "All sources"  # human label shown in headers and details

    @classmethod
    def everything(cls, label: str = "All sources") -> QueryScope:
        return cls(ScopeKind.EVERYTHING, label=label)

    @classmethod
    def for_folder(cls, path: str | Path, label: str | None = None) -> QueryScope:
        text_path = str(path)
        return cls(ScopeKind.FOLDER, folder=text_path, label=label or (Path(text_path).name or text_path))

    @classmethod
    def for_source(cls, source_id: str, label: str | None = None) -> QueryScope:
        return cls(ScopeKind.SOURCE, source_id=source_id, label=label or source_id)


@dataclass(frozen=True)
class ResolvedScope:
    """`version_ids is None` means unrestricted. Otherwise it is the complete
    set of versions a question may touch; `empty` is True when that set is
    empty (the question must not run)."""

    kind: ScopeKind
    label: str
    file_count: int
    version_ids: tuple[str, ...] | None
    empty: bool
    # Why an empty scope is empty (AbstentionReason value); None when not empty.
    reason: str | None = None

    @property
    def unrestricted(self) -> bool:
        return self.version_ids is None

    def contains(self, version_id: str) -> bool:
        return self.version_ids is None or version_id in self.version_ids


def _norm(path: str) -> tuple[str, ...]:
    return Path(os.path.normcase(os.path.abspath(os.path.expanduser(path)))).parts


def _folder_candidates(folder: str) -> list[tuple[str, ...]]:
    """The folder as typed and with symlinks resolved, so either spelling of
    the same place matches the stored paths."""
    found = [_norm(folder)]
    try:
        real = _norm(os.path.realpath(os.path.expanduser(folder)))
    except OSError:
        real = found[0]
    if real not in found:
        found.append(real)
    return found


def _inside(file_parts: tuple[str, ...], folder_parts: tuple[str, ...]) -> bool:
    return file_parts[: len(folder_parts)] == folder_parts


def resolve(engine: Engine, scope: QueryScope) -> ResolvedScope:
    """Resolve `scope` against the current index state."""
    versions = load_eligible_versions(engine)

    if scope.kind == ScopeKind.EVERYTHING:
        return ResolvedScope(
            kind=scope.kind, label=scope.label, file_count=len(versions),
            version_ids=None, empty=False,
        )

    if scope.kind == ScopeKind.SOURCE:
        ids = tuple(v.version_id for v in versions if v.source_id == scope.source_id)
        reason = None
        if not ids:
            reason = _source_reason(engine, scope.source_id)
        return ResolvedScope(
            kind=scope.kind, label=scope.label, file_count=len(ids),
            version_ids=ids, empty=not ids, reason=reason,
        )

    if scope.kind == ScopeKind.FOLDER:
        if not scope.folder:
            return ResolvedScope(
                kind=scope.kind, label=scope.label, file_count=0, version_ids=(), empty=True,
                reason=AbstentionReason.NO_EVIDENCE.value,
            )
        candidates = _folder_candidates(scope.folder)
        ids = tuple(
            v.version_id
            for v in versions
            if any(_inside(_norm(v.path), c) for c in candidates)
        )
        return ResolvedScope(
            kind=scope.kind, label=scope.label, file_count=len(ids), version_ids=ids,
            empty=not ids,
            reason=None if ids else AbstentionReason.NO_EVIDENCE.value,
        )

    raise ValueError(f"unknown scope kind: {scope.kind!r}")  # pragma: no cover


def _source_reason(engine: Engine, source_id: str | None) -> str:
    """SOURCE_UNAVAILABLE when the source is missing or not ACTIVE (revoked,
    unreachable), NO_EVIDENCE when it is active but nothing is READY yet."""
    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM sources WHERE id = :id"), {"id": source_id}
        ).scalar()
    if status is None or status != SourceStatus.ACTIVE.name:
        return AbstentionReason.SOURCE_UNAVAILABLE.value
    return AbstentionReason.NO_EVIDENCE.value


def empty_scope_message(scope: ResolvedScope) -> str:
    if scope.reason == AbstentionReason.SOURCE_UNAVAILABLE.value:
        return f"{scope.label} is not available right now, so nothing in it was searched."
    return f"Nothing is indexed in {scope.label} yet."
