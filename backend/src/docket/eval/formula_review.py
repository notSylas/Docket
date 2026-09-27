"""Human verification of unverified formula transcriptions (Phase B
checkpoint 2 of "verified formula transcription").

Checkpoint 1 (`docket.parsing.formula_crop`, wired into
`docket.ingestion.formula_transcriber.FormulaTranscriber.transcribe`)
crops each above-threshold detected formula region and stores an UNVERIFIED
VLM transcription on `EvidenceVersion.formula_transcriptions_json`, traceable
back to its source region (`EvidenceVersion.formula_regions_json`) via
`item_ref`. This module never touches that pipeline, `Chunk.text`, the
FTS/vector indexes, or `EvidenceResolver` -- it only samples a handful of
those already-stored transcriptions, re-crops the exact same source pixels a
human (or a blind-reviewing model) can compare them against, and later
aggregates a human's `correct: true/false` labels. There is no promotion
path here: turning a labeled-correct transcription into searchable/citable
evidence is a separate, later, explicitly gated decision this module does not
make (see `Docs/accuracy-evaluation.md`'s "Formula evidence and experiments"
section).

Modeled closely on `docket.eval.calibration`'s export/score pair, but
simpler: `calibration.py` measures agreement between an automatic judge
verdict and a human label (hence Cohen's kappa, hence rejoining a separate
judged-JSONL file at score time). Here there is no automatic verdict to
compare against -- a human's `correct: true/false` IS the verification --
so scoring is just aggregation, and everything scoring needs (the
transcription, the crop path, the id) already lives in the labels YAML
itself. A cheap per-item content fingerprint (hash of the load-bearing
export fields) still guards against a labels file whose `crop_path`/
`transcription`/`id` fields were hand-edited or corrupted after export,
without the full rigor of calibration's `record_fingerprint`
rejoin-and-compare (there is no second artifact to rejoin against).

The sampling/YAML-loading machinery both modules share lives in
`docket.eval.review` (see that module's docstring); this module is a thin
wrapper around it plus this checkpoint's own domain logic (candidate
collection, cropping, the fingerprint tamper-check).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from docket.core.db.models import EvidenceVersion
from docket.eval.review import DEFAULT_SAMPLE_SIZE, ReviewResult, bucket_sample, load_labels_yaml
from docket.infra.evidence.store import ContentAddressedStore
from docket.infra.parsing.formula_crop import crop_formula_region

_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9]+")


class FormulaReviewError(ValueError):
    """Malformed labels file, or nothing to export/score."""


def derive_id(evidence_version_id: str, item_ref: str | None) -> str:
    """A filesystem/YAML-safe id for one sampled region: the owning
    EvidenceVersion plus its `item_ref` (e.g. `#/texts/12`), with every
    non-alphanumeric run collapsed to a single `-` so it's also safe as a
    PNG filename."""
    ref_part = _ID_SANITIZE_RE.sub("-", item_ref or "unknown").strip("-") or "unknown"
    return f"{evidence_version_id}_{ref_part}"


def _resolve_page_hash(page_images_json: str | None, page_no: object) -> str | None:
    """Look up `page_no`'s content_hash in a decoded `page_images_json`
    mapping, tolerating both int and (post-JSON-round-trip) string keys --
    same accommodation `FormulaTranscriber.transcribe`
    makes for the same reason."""
    if not page_images_json:
        return None
    hashes = json.loads(page_images_json)
    content_hash = hashes.get(page_no)
    if content_hash is None:
        content_hash = hashes.get(str(page_no))
    return content_hash


@dataclass
class FormulaRegionCandidate:
    """One transcribed formula region eligible for export: a transcription
    from `formula_transcriptions_json` joined back to its source region in
    `formula_regions_json` via `item_ref`."""

    evidence_version_id: str
    source_id: str
    file_path: str | None
    item_ref: str | None
    page_no: object
    region: dict
    transcription: str | None
    model: str | None
    page_images_json: str | None


def collect_candidates(
    session_factory: sessionmaker, *, source_id: str | None = None
) -> list[FormulaRegionCandidate]:
    """Every transcribed formula region across current EvidenceVersion rows
    (optionally scoped to one source), joining `formula_transcriptions_json`
    entries back to their source region in `formula_regions_json` by
    `item_ref`.

    Only rows with a populated `formula_transcriptions_json` are considered,
    and only its entries (i.e. only regions that cleared checkpoint 1's size
    threshold and actually got a VLM call) ever become candidates -- a
    region present in `formula_regions_json` with no matching transcription
    entry is never visited here, which is exactly "skipped, not an error":
    there is nothing to look up in a set this function never iterates.
    A transcription entry whose `item_ref` isn't found in
    `formula_regions_json` (shouldn't happen given how checkpoint 1 builds
    both columns from the same regions, but defensive rather than assumed
    impossible) is skipped the same way.

    Scoped to `is_current` versions only, same posture as
    `EvidenceManager._current_version`/`EvidenceResolver`: a superseded
    version's transcriptions are stale evidence, not worth a human's time.
    """
    candidates: list[FormulaRegionCandidate] = []
    with session_factory() as session:
        stmt = select(EvidenceVersion).where(EvidenceVersion.is_current.is_(True))
        if source_id is not None:
            stmt = stmt.where(EvidenceVersion.source_id == source_id)
        for version in session.execute(stmt).scalars().all():
            if not version.formula_transcriptions_json or not version.formula_regions_json:
                continue
            transcriptions = json.loads(version.formula_transcriptions_json)
            regions_by_ref = {
                region.get("item_ref"): region
                for region in json.loads(version.formula_regions_json)
            }
            for entry in transcriptions:
                region = regions_by_ref.get(entry.get("item_ref"))
                if region is None:
                    continue
                candidates.append(
                    FormulaRegionCandidate(
                        evidence_version_id=version.id,
                        source_id=version.source_id,
                        file_path=version.file_path,
                        item_ref=entry.get("item_ref"),
                        page_no=entry.get("page_no", region.get("page_no")),
                        region=region,
                        transcription=entry.get("transcription"),
                        model=entry.get("model"),
                        page_images_json=version.page_images_json,
                    )
                )
    return candidates


def sample_candidates(
    candidates: list[FormulaRegionCandidate], n: int, seed: int = 0
) -> list[FormulaRegionCandidate]:
    """Round-robin over source documents (`source_id`, `file_path`) so a
    ~30-item sample spreads across documents/pages instead of clustering
    inside whichever document happens to have the most formula regions --
    same rationale as `eval/calibration.py`'s `sample_runs` round-robin (both
    now built on `eval.review.bucket_sample`), but stratified by document
    rather than by (judge|deterministic) x (pass|fail|doubt): there's no
    automatic-verdict dimension here worth stratifying on (every candidate
    already got a transcription; nothing distinguishes them a priori), but
    which *document* a region comes from is the obvious axis a plain
    reservoir/random sample could accidentally ignore (e.g. one 80-page
    report contributing every item while a 3-formula source contributes
    none)."""
    return bucket_sample(
        candidates,
        n,
        bucket_key=lambda c: (c.source_id, c.file_path),
        sort_key=lambda c: (c.source_id, c.file_path or "", str(c.page_no or ""), c.item_ref or ""),
        order_key=lambda key: (key[0], key[1] or ""),
        seed=seed,
    )


def _item_fingerprint(entry: dict) -> str:
    """Stable digest over an exported item's load-bearing fields (not
    `correct`/`notes`, which the labeler fills in) -- catches a labels file
    whose `crop_path`/`transcription`/`id` was hand-edited or corrupted
    between export and score, without the full rejoin-a-second-artifact
    rigor of `eval.schema.fingerprint`/`record_fingerprint` (there's no
    second artifact here to rejoin against; everything score needs already
    lives in this same file)."""
    payload = {k: entry.get(k) for k in ("id", "source_document", "page_no", "crop_path", "transcription")}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_LABELS_HEADER = (
    "# For each item below, open its `crop_path` (relative to this file) and compare\n"
    "# the image against `transcription`. Mark `correct: true` only if the\n"
    "# transcription exactly matches the crop -- including signs, exponents,\n"
    "# subscripts, vectors/hats, and units -- else `correct: false` and say what's\n"
    "# wrong in `notes` (e.g. \"dropped the minus sign\", \"subscript i missing\",\n"
    "# \"unit should be m/s not m/s^2\"). Agreement between models is not\n"
    "# verification; a human eyeballing the actual crop is.\n"
)


def export_labels(
    session_factory: sessionmaker,
    store: ContentAddressedStore,
    out_dir: Path,
    *,
    source_id: str | None = None,
    n: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
) -> int:
    """Sample up to `n` transcribed formula regions, crop each one (via the
    exact same `crop_formula_region` checkpoint 1 uses) to
    `<out_dir>/crops/<id>.png`, and write `<out_dir>/labels.yaml` for a human
    to fill in. Returns the number of items actually exported (may be less
    than `n`/the candidate count if a sampled region's page image can't be
    found in `store` -- skipped, not an error, same posture as
    `FormulaTranscriber.transcribe`'s own "no stored image
    for this page" skip).

    Raises `FormulaReviewError` if there are no candidates at all (nothing
    to export -- most likely `formula_transcription_enabled` was never
    turned on, or `source_id` doesn't match any transcribed source)."""
    candidates = collect_candidates(session_factory, source_id=source_id)
    if not candidates:
        scope = f" for source {source_id!r}" if source_id else ""
        raise FormulaReviewError(
            f"no transcribed formula regions found{scope} -- nothing to export "
            "(is formula_transcription_enabled on, and has this source been (re-)ingested since?)"
        )

    out_dir = Path(out_dir)
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict] = []
    for candidate in sample_candidates(candidates, n, seed):
        page_hash = _resolve_page_hash(candidate.page_images_json, candidate.page_no)
        if page_hash is None:
            continue
        page_image_bytes = store.get(page_hash)
        crop_bytes = crop_formula_region(page_image_bytes, candidate.region)

        item_id = derive_id(candidate.evidence_version_id, candidate.item_ref)
        crop_rel_path = Path("crops") / f"{item_id}.png"
        (out_dir / crop_rel_path).write_bytes(crop_bytes)

        entry = {
            "id": item_id,
            "source_document": candidate.file_path,
            "page_no": candidate.page_no,
            "crop_path": crop_rel_path.as_posix(),
            "transcription": candidate.transcription,
            "correct": None,
            "notes": "",
        }
        entry["fingerprint"] = _item_fingerprint(entry)
        items.append(entry)

    labels_path = out_dir / "labels.yaml"
    labels_path.write_text(
        _LABELS_HEADER
        + yaml.safe_dump({"version": 1, "items": items}, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return len(items)


def _check_fingerprint(item: dict) -> None:
    if item.get("fingerprint") is not None and item["fingerprint"] != _item_fingerprint(item):
        raise FormulaReviewError(
            f"item {item['id']!r} looks tampered with or corrupted: id/crop_path/transcription "
            "no longer match this item's fingerprint"
        )


def load_labels(path: Path) -> list[dict]:
    return load_labels_yaml(
        path,
        error_cls=FormulaReviewError,
        duplicate_label="formula review label",
        validate_item=_check_fingerprint,
    )


@dataclass
class FormulaReviewResult(ReviewResult):
    total: int
    failures: list[dict] = field(default_factory=list)


def score_labels(labels_path: Path) -> FormulaReviewResult:
    """Aggregate a filled-in labels file: how many were labeled, the
    fraction marked `correct: true`, and every `false` item's `notes`/
    `crop_path`/`transcription` so a reviewer can act on the failures
    directly. No kappa/verdict comparison -- a human's `correct: true/false`
    IS the verification here, there's nothing automatic to compare it
    against (unlike `eval.calibration.score_labels`)."""
    items = load_labels(labels_path)
    labeled = [item for item in items if item.get("correct") is not None]
    if not labeled:
        raise FormulaReviewError(f"no labeled items in {labels_path}: fill in `correct: true/false` first")

    failures = [
        {
            "id": item["id"],
            "crop_path": item.get("crop_path"),
            "transcription": item.get("transcription"),
            "notes": item.get("notes") or "",
        }
        for item in labeled
        if not item["correct"]
    ]
    correct_count = len(labeled) - len(failures)
    return FormulaReviewResult(
        total=len(items),
        labeled=len(labeled),
        unlabeled=len(items) - len(labeled),
        agreement=correct_count / len(labeled),
        failures=failures,
    )


def format_review(result: FormulaReviewResult) -> str:
    lines = [
        f"Labeled: {result.labeled}/{result.total} ({result.unlabeled} left unlabeled)",
        f"Agreement: {result.agreement:.1%} ({result.labeled - len(result.failures)}/{result.labeled} marked correct)",
    ]
    if result.failures:
        lines.append("\nIncorrect transcriptions:")
        for failure in result.failures:
            lines.append(f"  - {failure['id']} ({failure['crop_path']})")
            lines.append(f"      transcription: {failure['transcription']}")
            lines.append(f"      notes: {failure['notes']}")
    return "\n".join(lines)
