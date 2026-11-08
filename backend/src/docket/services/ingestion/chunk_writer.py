"""`ChunkWriter` -- persists parsed content as `EvidenceUnit`/`Chunk` rows and
answers the two lookups `IngestionPipeline` needs around that: the id of a
source+file's *current* `EvidenceVersion` (used for the before/after
unchanged-vs-changed snapshot -- see `docket.services.ingestion.pipeline`'s module
docstring, point 1) and the full set of chunk_ids currently reachable from a
source's current versions (used for the single end-of-run reconcile pass --
see that same docstring, point 2). Extracted from `IngestionPipeline` (Phase
3 of the restructuring) purely to shrink that file and make this slice of
persistence logic independently testable; no behavior changed in the move.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from docket.core.db.identity import compute_chunk_id
from docket.core.db.models import (
    Chunk,
    ChunkAlias,
    KEYWORD_SERVABLE,
    ChunkRecipe as ChunkRecipeRow,
    EvidenceUnit,
    EvidenceVersion,
    VersionStatus,
    _new_id,
)
from docket.infra.index.base import ChunkRecord
from docket.infra.index.context import index_text_for_chunk
from docket.infra.parsing.recipes import ChunkRecipe


@dataclass
class BuiltRows:
    """Unit/chunk rows (ids assigned) and index records for one version,
    built without touching the database, so embedding and vector writes can
    happen before the single SQLite transaction that swaps them in."""

    units: list
    chunks: list
    records: list


_ALIAS_SHINGLE = 6
_ALIAS_MIN_OVERLAP = 0.3


def map_overlapping_chunks(old: dict[str, str], new: dict[str, str]) -> dict[str, tuple[str | None, float]]:
    """For each old chunk id, the new chunk id sharing the most word
    6-grams with it and the share of the old chunk's 6-grams that it holds;
    `(None, share)` below `_ALIAS_MIN_OVERLAP` (the text moved or changed too
    much for a citation to still point at it)."""
    def shingles(text: str) -> set[tuple[str, ...]]:
        words = text.lower().split()
        if len(words) < _ALIAS_SHINGLE:
            return {tuple(words)} if words else set()
        return {tuple(words[i : i + _ALIAS_SHINGLE]) for i in range(len(words) - _ALIAS_SHINGLE + 1)}

    index: dict[tuple[str, ...], list[str]] = {}
    for nid, text in new.items():
        for sh in shingles(text):
            index.setdefault(sh, []).append(nid)
    out: dict[str, tuple[str | None, float]] = {}
    for oid, text in old.items():
        shs = shingles(text)
        votes: dict[str, int] = {}
        for sh in shs:
            for nid in index.get(sh, ()):
                votes[nid] = votes.get(nid, 0) + 1
        if not votes or not shs:
            out[oid] = (None, 0.0)
            continue
        best = max(votes, key=lambda k: (votes[k], k))
        share = votes[best] / len(shs)
        out[oid] = (best if share >= _ALIAS_MIN_OVERLAP else None, share)
    return out


class ChunkWriter:
    def __init__(self, *, session_factory: sessionmaker, chunk_recipe: ChunkRecipe) -> None:
        self._session_factory = session_factory
        self._chunk_recipe = chunk_recipe

    def ensure_recipe_row(self, recipe: ChunkRecipe | None = None) -> None:
        """Get-or-create the `ChunkRecipe` row for `recipe` (default: the
        writer's own recipe)."""
        recipe = recipe or self._chunk_recipe
        with self._session_factory() as session:
            existing = session.get(ChunkRecipeRow, recipe.id)
            if existing is not None:
                return
            session.add(
                ChunkRecipeRow(
                    id=recipe.id,
                    chunk_size=recipe.chunk_size,
                    overlap=recipe.overlap,
                    splitter=recipe.splitter,
                    parser_name=recipe.parser_name,
                    parser_version=recipe.parser_version,
                )
            )
            session.commit()

    def current_version_id(self, source_id: str, file_path: str) -> str | None:
        # Scoped by (source_id, file_path): a "local_folder" source can hold
        # many files, each with its own independent version lineage -- see
        # `EvidenceManager._current_version` and migration 0002's docstring
        # for why `source_id` alone isn't enough to identify "the" current
        # version once a source has more than one file.
        #
        # "Current" = latest lineage slot (status != SUPERSEDED), matching
        # `EvidenceManager._current_version`'s own posture -- this is the
        # before/after snapshot `IngestionPipeline` compares its post-ingest
        # id against, so it has to use the same notion of "current" or the
        # unchanged-vs-changed detection breaks. A PENDING or FAILED row is
        # still the current lineage slot even though it isn't servable yet.
        with self._session_factory() as session:
            stmt = select(EvidenceVersion.id).where(
                EvidenceVersion.source_id == source_id,
                EvidenceVersion.status != VersionStatus.SUPERSEDED,
                EvidenceVersion.file_path == file_path,
            )
            return session.execute(stmt).scalar_one_or_none()

    def chunk_ids_for_version(self, evidence_version_id: str) -> list[str]:
        with self._session_factory() as session:
            stmt = select(Chunk.id).where(Chunk.evidence_version_id == evidence_version_id)
            return list(session.execute(stmt).scalars().all())

    def delete_units_and_chunks(self, evidence_version_id: str) -> None:
        """Remove one version's `Chunk` and `EvidenceUnit` rows in a single
        transaction (chunks first: they reference units). Used before a
        retry rebuilds them -- chunk ids are deterministic, so leftover rows
        from a failed attempt would collide on `chunks.id`, and unit ids are
        random, so they would otherwise be duplicated. Blob references are
        untouched: none are keyed by chunk/unit rows (they belong to the
        version's content and page images)."""
        with self._session_factory() as session:
            session.execute(delete(Chunk).where(Chunk.evidence_version_id == evidence_version_id))
            session.execute(
                delete(EvidenceUnit).where(EvidenceUnit.evidence_version_id == evidence_version_id)
            )
            session.commit()

    def chunk_recipe_ids_by_version(self, source_id: str) -> dict[str, set[str]]:
        """`{evidence_version_id: {recipe ids stamped on its chunks}}` for
        every version of `source_id` that has at least one chunk."""
        with self._session_factory() as session:
            stmt = (
                select(Chunk.evidence_version_id, Chunk.chunk_recipe_id)
                .where(Chunk.source_id == source_id)
                .distinct()
            )
            out: dict[str, set[str]] = {}
            for version_id, recipe_id in session.execute(stmt).all():
                out.setdefault(version_id, set()).add(recipe_id)
            return out

    def persist_units_and_chunks(
        self,
        *,
        source_id: str,
        evidence_version_id: str,
        units,
        chunks,
        recipe: ChunkRecipe | None = None,
        provisional: bool = False,
    ) -> list[ChunkRecord]:
        recipe = recipe or self._chunk_recipe
        with self._session_factory() as session:
            unit_objs = [
                EvidenceUnit(
                    evidence_version_id=evidence_version_id,
                    unit_index=u.unit_index,
                    heading=u.heading,
                    content_hash=u.content_hash,
                    unit_kind=u.unit_kind,
                    locator_json=u.locator_json,
                )
                for u in units
            ]
            session.add_all(unit_objs)
            session.flush()
            file_path = session.get(EvidenceVersion, evidence_version_id).file_path
            locator_by_unit_id = {obj.id: obj.locator_json for obj in unit_objs}
            unit_id_by_index = {
                u.unit_index: obj.id for u, obj in zip(units, unit_objs)
            }

            chunk_objs = []
            for c in chunks:
                chunk_id = compute_chunk_id(
                    evidence_version_id, recipe.id, c.ordinal, c.content_hash
                )
                chunk_objs.append(
                    Chunk(
                        id=chunk_id,
                        source_id=source_id,
                        evidence_version_id=evidence_version_id,
                        evidence_unit_id=unit_id_by_index[c.evidence_unit_index],
                        chunk_recipe_id=recipe.id,
                        ordinal=c.ordinal,
                        heading=c.heading,
                        text=c.text,
                        content_hash=c.content_hash,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        provenance=c.provenance,
                        provisional=provisional,
                    )
                )
            session.add_all(chunk_objs)
            session.commit()

            return [
                ChunkRecord(
                    chunk_id=c.id,
                    source_id=c.source_id,
                    evidence_version_id=c.evidence_version_id,
                    evidence_unit_id=c.evidence_unit_id,
                    chunk_recipe_id=c.chunk_recipe_id,
                    ordinal=c.ordinal,
                    heading=c.heading,
                    text=c.text,
                    content_hash=c.content_hash,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    index_text=index_text_for_chunk(
                        file_path, locator_by_unit_id[c.evidence_unit_id], c.heading, c.text
                    ),
                )
                for c in chunk_objs
            ]

    # -- tiered indexing ------------------------------------------------------

    def build_rows(
        self,
        *,
        source_id: str,
        evidence_version_id: str,
        file_path: str | None,
        units,
        chunks,
        recipe: ChunkRecipe | None = None,
        provisional: bool = False,
    ) -> BuiltRows:
        """Same rows `persist_units_and_chunks` writes, built in memory (unit
        ids are assigned here instead of at flush time)."""
        recipe = recipe or self._chunk_recipe
        unit_objs = [
            EvidenceUnit(
                id=_new_id("eu"),
                evidence_version_id=evidence_version_id,
                unit_index=u.unit_index,
                heading=u.heading,
                content_hash=u.content_hash,
                unit_kind=u.unit_kind,
                locator_json=u.locator_json,
            )
            for u in units
        ]
        unit_by_index = {u.unit_index: o for u, o in zip(units, unit_objs)}
        chunk_objs, records = [], []
        for c in chunks:
            unit = unit_by_index[c.evidence_unit_index]
            chunk_id = compute_chunk_id(evidence_version_id, recipe.id, c.ordinal, c.content_hash)
            chunk_objs.append(
                Chunk(
                    id=chunk_id, source_id=source_id, evidence_version_id=evidence_version_id,
                    evidence_unit_id=unit.id, chunk_recipe_id=recipe.id, ordinal=c.ordinal,
                    heading=c.heading, text=c.text, content_hash=c.content_hash,
                    page_start=c.page_start, page_end=c.page_end, provenance=c.provenance,
                    provisional=provisional,
                )
            )
            records.append(
                ChunkRecord(
                    chunk_id=chunk_id, source_id=source_id, evidence_version_id=evidence_version_id,
                    evidence_unit_id=unit.id, chunk_recipe_id=recipe.id, ordinal=c.ordinal,
                    heading=c.heading, text=c.text, content_hash=c.content_hash,
                    page_start=c.page_start, page_end=c.page_end,
                    index_text=index_text_for_chunk(file_path, unit.locator_json, c.heading, c.text),
                )
            )
        return BuiltRows(units=unit_objs, chunks=chunk_objs, records=records)

    def has_provisional(self, evidence_version_id: str) -> bool:
        with self._session_factory() as session:
            return (
                session.execute(
                    select(Chunk.id)
                    .where(Chunk.evidence_version_id == evidence_version_id, Chunk.provisional.is_(True))
                    .limit(1)
                ).first()
                is not None
            )

    def replace_version_content(
        self,
        *,
        fts,
        evidence_version_id: str,
        built: BuiltRows,
        new_status: VersionStatus,
        record_aliases: bool = False,
    ) -> int | None:
        """Swap a version's whole content in ONE SQLite transaction: delete
        its old chunk/unit rows and their FTS rows (found through
        `fts_chunk_map`), insert `built` (rows + FTS rows) and set the
        version's status. Nothing is visible half-done and an exception at any
        point rolls everything back, leaving the previous content (and its
        status) exactly as it was. Returns the number of chunks written, or
        `None` (nothing changed) when the version has meanwhile been
        superseded or removed. Vector rows are NOT touched here: the caller
        appends them before this call (new ids are invisible until the status
        says READY) and reconcile removes any that a crash left behind."""
        from sqlalchemy import text as sql_text

        with self._session_factory() as session:
            version = session.get(EvidenceVersion, evidence_version_id)
            if version is None or version.status == VersionStatus.SUPERSEDED:
                return None
            conn = session.connection()
            old = {}
            if record_aliases:
                old = {
                    cid: txt
                    for cid, txt in session.execute(
                        select(Chunk.id, Chunk.text).where(
                            Chunk.evidence_version_id == evidence_version_id,
                            Chunk.provisional.is_(True),
                        )
                    ).all()
                }
            fts.delete_version_in(conn, evidence_version_id)
            session.execute(delete(Chunk).where(Chunk.evidence_version_id == evidence_version_id))
            session.execute(
                delete(EvidenceUnit).where(EvidenceUnit.evidence_version_id == evidence_version_id)
            )
            session.flush()
            session.add_all(built.units)
            session.flush()
            session.add_all(built.chunks)
            session.flush()
            fts.upsert_in(conn, built.records)
            new_ids = {c.id for c in built.chunks}
            if record_aliases and old:
                mapping = map_overlapping_chunks(
                    {k: v for k, v in old.items() if k not in new_ids},
                    {c.id: c.text for c in built.chunks},
                )
                for oid, (nid, share) in mapping.items():
                    session.merge(
                        ChunkAlias(
                            old_chunk_id=oid, new_chunk_id=nid,
                            evidence_version_id=evidence_version_id, overlap=share,
                        )
                    )
            if new_ids:
                session.execute(delete(ChunkAlias).where(ChunkAlias.old_chunk_id.in_(new_ids)))
            version.status = new_status
            version.failure_reason = None
            session.commit()
            return len(built.chunks)

    def find_clone_donor(
        self, *, content_hash: str, exclude_version_id: str, parser_name: str,
        parser_version: str, recipe_id: str,
    ) -> str | None:
        """Id of a READY version with identical bytes, parsed by the same
        parser, whose chunks were all cut with `recipe_id` (and that has
        its regions/images bookkeeping recorded): a safe source to clone
        parsed units/chunks from instead of parsing again."""
        with self._session_factory() as session:
            stmt = (
                select(EvidenceVersion.id)
                .where(
                    EvidenceVersion.content_hash == content_hash,
                    EvidenceVersion.id != exclude_version_id,
                    EvidenceVersion.status == VersionStatus.READY,
                    EvidenceVersion.parser_name == parser_name,
                    EvidenceVersion.parser_version == parser_version,
                    EvidenceVersion.formula_regions_json.is_not(None),
                )
                .order_by(EvidenceVersion.observed_at)
            )
            for donor_id in session.execute(stmt).scalars().all():
                recipes = set(
                    session.execute(
                        select(Chunk.chunk_recipe_id)
                        .where(Chunk.evidence_version_id == donor_id)
                        .distinct()
                    ).scalars()
                )
                if recipes <= {recipe_id}:
                    return donor_id
        return None

    def clone_version(
        self, *, donor_version_id: str, new_version_id: str, recipe: ChunkRecipe | None = None
    ) -> tuple[list[ChunkRecord], dict[str, ChunkRecord]]:
        """Copy the donor's units and chunks onto `new_version_id` (new ids,
        same content), returning the new `ChunkRecord`s (index text computed
        for the NEW file path) and `{new chunk_id: donor ChunkRecord}` for
        vector reuse. Also copies the donor's formula-region and page-image
        bookkeeping (plus blob references)."""
        import json

        from docket.infra.evidence.references import add_blob_reference

        recipe = recipe or self._chunk_recipe
        with self._session_factory() as session:
            donor = session.get(EvidenceVersion, donor_version_id)
            new = session.get(EvidenceVersion, new_version_id)
            units = (
                session.execute(
                    select(EvidenceUnit)
                    .where(EvidenceUnit.evidence_version_id == donor_version_id)
                    .order_by(EvidenceUnit.unit_index)
                )
                .scalars()
                .all()
            )
            new_units = {}
            for u in units:
                nu = EvidenceUnit(
                    evidence_version_id=new_version_id, unit_index=u.unit_index,
                    heading=u.heading, content_hash=u.content_hash, unit_kind=u.unit_kind,
                    locator_json=u.locator_json,
                )
                session.add(nu)
                new_units[u.id] = nu
            session.flush()
            chunks = (
                session.execute(
                    select(Chunk).where(Chunk.evidence_version_id == donor_version_id).order_by(Chunk.ordinal)
                )
                .scalars()
                .all()
            )
            records: list[ChunkRecord] = []
            donor_records: dict[str, ChunkRecord] = {}
            for c in chunks:
                nid = compute_chunk_id(new_version_id, recipe.id, c.ordinal, c.content_hash)
                nu = new_units[c.evidence_unit_id]
                session.add(
                    Chunk(
                        id=nid, source_id=new.source_id, evidence_version_id=new_version_id,
                        evidence_unit_id=nu.id, chunk_recipe_id=recipe.id, ordinal=c.ordinal,
                        heading=c.heading, text=c.text, content_hash=c.content_hash,
                        page_start=c.page_start, page_end=c.page_end, provenance=c.provenance,
                    )
                )
                locator = nu.locator_json
                common = dict(
                    evidence_unit_id=nu.id, chunk_recipe_id=recipe.id, ordinal=c.ordinal,
                    heading=c.heading, text=c.text, content_hash=c.content_hash,
                    page_start=c.page_start, page_end=c.page_end,
                )
                rec = ChunkRecord(
                    chunk_id=nid, source_id=new.source_id, evidence_version_id=new_version_id,
                    index_text=index_text_for_chunk(new.file_path, locator, c.heading, c.text),
                    **common,
                )
                records.append(rec)
                donor_records[nid] = ChunkRecord(
                    chunk_id=c.id, source_id=c.source_id, evidence_version_id=donor_version_id,
                    index_text=index_text_for_chunk(donor.file_path, locator, c.heading, c.text),
                    **common,
                )
            new.formula_regions_json = donor.formula_regions_json
            if donor.page_images_json is not None:
                new.page_images_json = donor.page_images_json
                for page_no, h in json.loads(donor.page_images_json).items():
                    add_blob_reference(
                        session, content_hash=h, referencing_table="evidence_versions",
                        referencing_id=new_version_id, role=f"page_image:{page_no}",
                    )
            session.commit()
        return records, donor_records

    def ready_version_ids_for_source(self, source_id: str) -> set[str]:
        """Ids of `source_id`'s servable (`READY`) versions -- the "should
        still have visual pages indexed" set for the end-of-run reconcile."""
        with self._session_factory() as session:
            stmt = select(EvidenceVersion.id).where(
                EvidenceVersion.source_id == source_id,
                EvidenceVersion.status == VersionStatus.READY,
            )
            return set(session.execute(stmt).scalars().all())

    def current_chunk_ids_for_source(self, source_id: str) -> set[str]:
        """All chunk_ids reachable from `source_id`'s *servable* (`READY`, or
        `TEXT_READY` with its provisional FTS-only chunks)
        EvidenceVersions -- the "should still be indexed" set used for the
        single end-of-run reconcile pass.

        Deliberately `status == READY` here, not just "not superseded": a
        version whose chunking succeeded but whose index upsert then raised
        ends up `FAILED` with real `Chunk` rows already committed (chunk
        persistence commits before the index upsert that can fail) -- those
        orphaned rows must not be treated as "desired" index state, or
        reconcile would leave stale/partial index entries for a version
        nothing should be able to retrieve. A `PENDING`/`FAILED` version
        never legitimately owns chunks that *should* be indexed, so this is
        equivalent to "not superseded" in the common case and strictly safer
        in that one.
        """
        with self._session_factory() as session:
            stmt = (
                select(Chunk.id)
                .join(EvidenceVersion, Chunk.evidence_version_id == EvidenceVersion.id)
                .where(
                    EvidenceVersion.source_id == source_id,
                    EvidenceVersion.status.in_(KEYWORD_SERVABLE),
                )
            )
            return set(session.execute(stmt).scalars().all())
