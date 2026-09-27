"""`FormulaTranscriber` -- stores detected formula regions and, when
`settings.formula_transcription_enabled` is on, VLM-transcribes each
above-threshold region into an UNVERIFIED
`EvidenceVersion.formula_transcriptions_json`, never promoted into
searchable/citable evidence (see `Docs/accuracy-evaluation.md`'s "Formula
evidence and experiments" section). Extracted from `IngestionPipeline`
(Phase 3 of the restructuring) purely to shrink that file and make this
slice of the formula-transcription pass independently testable; no behavior
changed in the move.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import sessionmaker

from docket.core.config import Settings
from docket.core.db.models import EvidenceVersion
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.inference.gateway import InferenceGateway
from docket.parsing.formula_crop import crop_formula_region, is_transcribable
from docket.prompts.vision import FORMULA_TRANSCRIPTION_PROMPT


class FormulaTranscriber:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        store: ContentAddressedStore,
        gateway: InferenceGateway | None,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._gateway = gateway
        self._settings = settings

    def save_regions(self, version_id: str, regions: list[dict]) -> None:
        with self._session_factory() as session:
            version = session.get(EvidenceVersion, version_id)
            version.formula_regions_json = json.dumps(regions)
            session.commit()

    def transcribe(
        self,
        *,
        evidence_version_id: str,
        formula_regions: list[dict],
        page_image_hashes: dict,
    ) -> None:
        """VLM-transcribe each formula region above
        `formula_crop.MIN_FORMULA_REGION_AREA_PT2`, storing the result on
        `EvidenceVersion.formula_transcriptions_json` -- an UNVERIFIED
        experiment, never promoted into searchable/citable evidence (see
        `Docs/accuracy-evaluation.md`'s "Formula evidence and experiments"
        section). Only ever called when
        `settings.formula_transcription_enabled` is True (callers check
        that, not this method) -- `self._gateway` is required at that
        point; a misconfiguration (flag on, gateway not wired) should fail
        loudly rather than silently skip, same posture as
        `VisualIndexer.index_pages`.

        Depends on page images already being stored: `page_image_hashes` is
        `EvidenceVersion.page_images_json`'s mapping (either freshly
        returned by `VisualIndexer.save_page_images`, with int keys, or
        reloaded via `json.loads`, with string keys after the JSON
        round-trip -- both are handled below) -- this method never
        re-renders a page itself, only fetches already-stored bytes from
        the evidence store.

        Runs unconditionally once entered (even producing an empty `[]`)
        so `EvidenceVersion.formula_transcriptions_json` is never left
        `None` after this method runs -- same rationale as
        `VisualIndexer.save_page_images`'s unconditional save: marks this
        version as processed so the unchanged-file backfill branch doesn't
        keep retrying it every run.
        """
        if self._gateway is None:
            raise RuntimeError(
                "formula_transcription_enabled is True but IngestionPipeline "
                "was built without a gateway"
            )

        transcriptions: list[dict] = []
        for region in formula_regions:
            if not is_transcribable(region):
                continue
            page_no = region.get("page_no")
            content_hash = page_image_hashes.get(page_no)
            if content_hash is None:
                content_hash = page_image_hashes.get(str(page_no))
            if content_hash is None:
                # No stored image for this region's page (e.g. that page's
                # image generation failed, see `_page_images`) -- nothing to
                # crop from, skip rather than error.
                continue
            page_image_bytes = self._store.get(content_hash)
            crop_bytes = crop_formula_region(page_image_bytes, region)
            transcription = self._gateway.describe_image(
                crop_bytes,
                prompt=FORMULA_TRANSCRIPTION_PROMPT,
                model=self._settings.vision_model,
            )
            transcriptions.append({
                "item_ref": region.get("item_ref"),
                "page_no": page_no,
                "transcription": transcription,
                "model": self._settings.vision_model,
            })
        self._save_transcriptions(evidence_version_id, transcriptions)

    def _save_transcriptions(self, version_id: str, transcriptions: list[dict]) -> None:
        with self._session_factory() as session:
            version = session.get(EvidenceVersion, version_id)
            version.formula_transcriptions_json = json.dumps(transcriptions)
            session.commit()
