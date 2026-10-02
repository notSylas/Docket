"""Tests for `docket.eval.formula_review` -- Phase B checkpoint 2 of
"verified formula transcription": sampling already-stored (unverified) VLM
transcriptions, re-cropping their exact source pixels for a human to compare
against, and aggregating the human's true/false labels.

Fixtures build `EvidenceVersion` rows and `ContentAddressedStore` entries
directly (same style as `test_ingestion_pipeline.py`'s formula-transcription
tests), rather than running the real ingestion pipeline -- this module only
reads already-populated `formula_regions_json`/`formula_transcriptions_json`/
`page_images_json`, it never produces them.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from PIL import Image
from sqlalchemy import Engine

from docket.core.db.engine import get_session_factory
from docket.core.db.models import EvidenceVersion, VersionStatus
from docket.infra.evidence.store import ContentAddressedStore
from docket.eval.formula_review import (
    FormulaReviewError,
    export_labels,
    load_labels,
    score_labels,
)
from docket.services.sources.manager import SourceManager


def _page_png(width_px: int = 600, height_px: int = 800) -> bytes:
    image = Image.new("RGB", (width_px, height_px), (255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# Same real-shape fixture regions `test_ingestion_pipeline.py` uses.
_ABOVE_THRESHOLD_REGION = {
    "item_ref": "#/texts/1",
    "page_no": 1,
    "coordinate_origin": "BOTTOMLEFT",
    "page_width": 300.0,
    "page_height": 400.0,
    "bbox": {"l": 100.0, "t": 300.0, "r": 150.0, "b": 280.0},
}
_BELOW_THRESHOLD_REGION = {
    "item_ref": "#/texts/2",
    "page_no": 1,
    "coordinate_origin": "BOTTOMLEFT",
    "page_width": 300.0,
    "page_height": 400.0,
    "bbox": {"l": 10.0, "t": 20.0, "r": 19.0, "b": 10.0},
}


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedStore:
    root = tmp_path / "evidence_store"
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return ContentAddressedStore(root)


def _make_version(
    session_factory,
    store: ContentAddressedStore,
    source_id: str,
    *,
    file_path: str,
    regions: list[dict],
    transcriptions: list[dict] | None,
    page_images: dict[int, bytes] | None,
) -> EvidenceVersion:
    """Directly builds an EvidenceVersion row (no pipeline run) with
    `formula_regions_json`/`formula_transcriptions_json`/`page_images_json`
    populated exactly like `FormulaTranscriber.save_regions`/
    `VisualIndexer.save_page_images`/`FormulaTranscriber._save_transcriptions`
    would, plus the page PNGs actually stored in `store` so a real crop can
    happen."""
    page_images = page_images or {}
    hashes = {page_no: store.put(data) for page_no, data in page_images.items()}
    with session_factory() as session:
        version = EvidenceVersion(
            source_id=source_id,
            file_path=file_path,
            content_hash=f"hash-{file_path}",
            byte_size=1,
            parser_name="fixture",
            parser_version="1",
            formula_regions_json=json.dumps(regions),
            formula_transcriptions_json=json.dumps(transcriptions) if transcriptions is not None else None,
            page_images_json=json.dumps(hashes),
            status=VersionStatus.READY,
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        return version


@pytest.fixture()
def env(migrated_sqlite_engine: Engine, tmp_path: Path, store: ContentAddressedStore):
    from types import SimpleNamespace

    session_factory = get_session_factory(migrated_sqlite_engine)
    source_manager = SourceManager(session_factory)
    folder = tmp_path / "docs"
    folder.mkdir()
    source = source_manager.register_source(folder)
    return SimpleNamespace(session_factory=session_factory, store=store, source=source, folder=folder)


# ---------------------------------------------------------------------------
# export_labels
# ---------------------------------------------------------------------------


def test_export_writes_expected_item_count_crops_and_labels_shape(env, tmp_path: Path):
    transcriptions = [
        {"item_ref": "#/texts/1", "page_no": 1, "transcription": "V = IR", "model": "fake-vlm"},
    ]
    _make_version(
        env.session_factory,
        env.store,
        env.source.id,
        file_path=str(env.folder / "formulas.docx"),
        regions=[_ABOVE_THRESHOLD_REGION, _BELOW_THRESHOLD_REGION],
        transcriptions=transcriptions,
        page_images={1: _page_png()},
    )

    out_dir = tmp_path / "review"
    count = export_labels(env.session_factory, env.store, out_dir, n=30, seed=0)

    assert count == 1

    labels_path = out_dir / "labels.yaml"
    assert labels_path.exists()
    raw = yaml.safe_load(labels_path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["items"]) == 1

    item = raw["items"][0]
    assert item["source_document"] == str(env.folder / "formulas.docx")
    assert item["page_no"] == 1
    assert item["transcription"] == "V = IR"
    assert item["correct"] is None
    assert item["notes"] == ""
    assert "fingerprint" in item

    crop_path = out_dir / item["crop_path"]
    assert crop_path.exists()
    with Image.open(crop_path) as image:
        image.verify()


def test_export_skips_region_with_no_matching_transcription(env, tmp_path: Path):
    """Only the above-threshold region got a transcription (as checkpoint 1
    would produce); the below-threshold region must be skipped, not error."""
    transcriptions = [
        {"item_ref": "#/texts/1", "page_no": 1, "transcription": "V = IR", "model": "fake-vlm"},
    ]
    _make_version(
        env.session_factory,
        env.store,
        env.source.id,
        file_path=str(env.folder / "formulas.docx"),
        regions=[_ABOVE_THRESHOLD_REGION, _BELOW_THRESHOLD_REGION],
        transcriptions=transcriptions,
        page_images={1: _page_png()},
    )

    out_dir = tmp_path / "review"
    export_labels(env.session_factory, env.store, out_dir)

    raw = yaml.safe_load((out_dir / "labels.yaml").read_text(encoding="utf-8"))
    refs = {item["id"] for item in raw["items"]}
    assert len(raw["items"]) == 1
    # #/texts/2 (below threshold, never transcribed) must not appear.
    assert all("texts-2" not in ref for ref in refs)


def test_export_raises_when_no_transcriptions_exist(env, tmp_path: Path):
    _make_version(
        env.session_factory,
        env.store,
        env.source.id,
        file_path=str(env.folder / "formulas.docx"),
        regions=[_ABOVE_THRESHOLD_REGION],
        transcriptions=None,
        page_images={1: _page_png()},
    )

    with pytest.raises(FormulaReviewError):
        export_labels(env.session_factory, env.store, tmp_path / "review")


def test_export_scoped_to_source_id_ignores_other_sources(env, tmp_path: Path):
    from docket.services.sources.manager import SourceManager

    other_folder = tmp_path / "other-docs"
    other_folder.mkdir()
    other_source = SourceManager(env.session_factory).register_source(other_folder)

    _make_version(
        env.session_factory, env.store, env.source.id,
        file_path=str(env.folder / "a.docx"),
        regions=[_ABOVE_THRESHOLD_REGION],
        transcriptions=[{"item_ref": "#/texts/1", "page_no": 1, "transcription": "A", "model": "m"}],
        page_images={1: _page_png()},
    )
    _make_version(
        env.session_factory, env.store, other_source.id,
        file_path=str(other_folder / "b.docx"),
        regions=[_ABOVE_THRESHOLD_REGION],
        transcriptions=[{"item_ref": "#/texts/1", "page_no": 1, "transcription": "B", "model": "m"}],
        page_images={1: _page_png()},
    )

    out_dir = tmp_path / "review"
    count = export_labels(env.session_factory, env.store, out_dir, source_id=env.source.id)
    assert count == 1
    raw = yaml.safe_load((out_dir / "labels.yaml").read_text(encoding="utf-8"))
    assert raw["items"][0]["transcription"] == "A"


def test_export_samples_across_documents_not_just_one(env, tmp_path: Path):
    """With more candidates than `n` spread across several documents, the
    sample must not come entirely from a single document."""
    for i in range(3):
        file_path = str(env.folder / f"doc{i}.docx")
        transcriptions = [
            {"item_ref": f"#/texts/{j}", "page_no": 1, "transcription": f"eq{i}-{j}", "model": "m"}
            for j in range(5)
        ]
        regions = [{**_ABOVE_THRESHOLD_REGION, "item_ref": f"#/texts/{j}"} for j in range(5)]
        _make_version(
            env.session_factory, env.store, env.source.id,
            file_path=file_path, regions=regions, transcriptions=transcriptions,
            page_images={1: _page_png()},
        )

    out_dir = tmp_path / "review"
    export_labels(env.session_factory, env.store, out_dir, n=3, seed=0)
    raw = yaml.safe_load((out_dir / "labels.yaml").read_text(encoding="utf-8"))
    docs = {item["source_document"] for item in raw["items"]}
    assert len(docs) == 3  # one from each document, not 3 from the same one


# ---------------------------------------------------------------------------
# score_labels
# ---------------------------------------------------------------------------


def _write_labels(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "items": items}, sort_keys=False), encoding="utf-8")


def test_score_reports_aggregate_numbers_and_failures_with_notes(tmp_path: Path):
    items = [
        {"id": "a", "source_document": "x.pdf", "page_no": 1, "crop_path": "crops/a.png",
         "transcription": "V = IR", "correct": True, "notes": ""},
        {"id": "b", "source_document": "x.pdf", "page_no": 2, "crop_path": "crops/b.png",
         "transcription": "E = mc^2", "correct": False, "notes": "exponent dropped, should be squared"},
        {"id": "c", "source_document": "x.pdf", "page_no": 3, "crop_path": "crops/c.png",
         "transcription": "F = ma", "correct": None, "notes": ""},
    ]
    labels_path = tmp_path / "labels.yaml"
    _write_labels(labels_path, items)

    result = score_labels(labels_path)

    assert result.total == 3
    assert result.labeled == 2
    assert result.unlabeled == 1
    assert result.agreement == pytest.approx(0.5)
    assert len(result.failures) == 1
    assert result.failures[0]["id"] == "b"
    assert result.failures[0]["notes"] == "exponent dropped, should be squared"
    assert result.failures[0]["crop_path"] == "crops/b.png"
    assert result.failures[0]["transcription"] == "E = mc^2"


def test_score_raises_when_nothing_labeled_yet(tmp_path: Path):
    items = [
        {"id": "a", "source_document": "x.pdf", "page_no": 1, "crop_path": "crops/a.png",
         "transcription": "V = IR", "correct": None, "notes": ""},
    ]
    labels_path = tmp_path / "labels.yaml"
    _write_labels(labels_path, items)

    with pytest.raises(FormulaReviewError):
        score_labels(labels_path)


def test_score_rejects_tampered_fingerprint(tmp_path: Path):
    from docket.eval.formula_review import _item_fingerprint

    entry = {
        "id": "a", "source_document": "x.pdf", "page_no": 1, "crop_path": "crops/a.png",
        "transcription": "V = IR", "correct": True, "notes": "",
    }
    entry["fingerprint"] = _item_fingerprint(entry)
    entry["transcription"] = "V = IQ"  # edited after fingerprinting
    labels_path = tmp_path / "labels.yaml"
    _write_labels(labels_path, [entry])

    with pytest.raises(FormulaReviewError):
        load_labels(labels_path)


def test_score_rejects_duplicate_ids(tmp_path: Path):
    items = [
        {"id": "a", "source_document": "x.pdf", "page_no": 1, "crop_path": "crops/a.png",
         "transcription": "V = IR", "correct": True, "notes": ""},
        {"id": "a", "source_document": "x.pdf", "page_no": 2, "crop_path": "crops/a2.png",
         "transcription": "F = ma", "correct": True, "notes": ""},
    ]
    labels_path = tmp_path / "labels.yaml"
    _write_labels(labels_path, items)

    with pytest.raises(FormulaReviewError):
        score_labels(labels_path)
