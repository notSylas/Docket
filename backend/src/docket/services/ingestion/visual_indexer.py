"""`VisualIndexer` -- stores each page's rendered image bytes and, when
`settings.visual_index_enabled` is on, describes them via the VLM and writes
the result to the `pages` LanceDB table for visual retrieval. Extracted from
`IngestionPipeline` (Phase 3 of the restructuring) purely to shrink that file
and make this slice of the visual-index pass independently testable; no
behavior changed in the move.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import sessionmaker

from docket.core.config import Settings
from docket.core.db.models import EvidenceVersion
from docket.infra.evidence.references import add_blob_reference
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.index.visual_index import LancePageIndexWriter, PageRecord
from docket.infra.inference.gateway import InferenceGateway
from docket.prompts.vision import PAGE_DESCRIPTION_PROMPT


class VisualIndexer:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        store: ContentAddressedStore,
        gateway: InferenceGateway | None,
        visual_index_writer: LancePageIndexWriter | None,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._gateway = gateway
        self._visual_index_writer = visual_index_writer
        self._settings = settings

    def save_page_images(self, version_id: str, page_images: dict[int, bytes]) -> dict[int, str]:
        """Store each page's PNG bytes in the same `ContentAddressedStore`
        instance `EvidenceManager` uses for raw document bytes (accessed via
        its public `.store` attribute -- no second store instance), and
        persist the resulting `{page_no: content_hash}` mapping on
        `EvidenceVersion.page_images_json`. Runs unconditionally (unlike VLM
        description/embedding, this storage step isn't gated behind
        `settings.visual_index_enabled`) -- an empty `page_images` dict is
        still saved as `"{}"`, marking this version as processed so the
        unchanged-file backfill branch above doesn't keep re-parsing it.

        Also records one `EvidenceBlobReference` row per page-image hash, in
        the SAME transaction as the `page_images_json` write (Upgrade doc 03
        section 8: `page_images_json` is a second, JSON-buried set of hashes
        into the same `ContentAddressedStore` as the primary document
        bytes, so each entry needs its own reference row, not just one row
        for the whole JSON blob). `add_blob_reference` is idempotent, so a
        retried/backfilled call with identical hashes doesn't insert
        duplicate rows."""
        hashes = {page_no: self._store.put(data) for page_no, data in page_images.items()}
        with self._session_factory() as session:
            version = session.get(EvidenceVersion, version_id)
            version.page_images_json = json.dumps(hashes)
            for page_no, content_hash in hashes.items():
                add_blob_reference(
                    session,
                    content_hash=content_hash,
                    referencing_table="evidence_versions",
                    referencing_id=version_id,
                    role=f"page_image:{page_no}",
                )
            session.commit()
        return hashes

    def index_pages(
        self, *, evidence_version_id: str, source_id: str, page_images: dict[int, bytes]
    ) -> None:
        """Describe each page image via the VLM and embed the description
        with the existing text embed model, writing one row per page to the
        `pages` LanceDB table. Only ever called when
        `settings.visual_index_enabled` is True (callers check that, not
        this method) -- `self._gateway`/`self._visual_index_writer` are
        required at that point; a misconfiguration (flag on, dependency not
        wired) should fail loudly rather than silently skip indexing."""
        if not page_images:
            return
        if self._gateway is None or self._visual_index_writer is None:
            raise RuntimeError(
                "visual_index_enabled is True but IngestionPipeline was built "
                "without a gateway/visual_index_writer"
            )

        records: list[PageRecord] = []
        embeddings: list[list[float]] = []
        for page_no, image_bytes in page_images.items():
            description = self._gateway.describe_image(
                image_bytes,
                prompt=PAGE_DESCRIPTION_PROMPT,
                model=self._settings.vision_model,
            )
            embeddings.append(self._gateway.embed(description))
            records.append(
                PageRecord(
                    evidence_version_id=evidence_version_id,
                    source_id=source_id,
                    page_no=page_no,
                    description=description,
                )
            )
        self._visual_index_writer.upsert(records, embeddings)
