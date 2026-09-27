"""Freeze benchmark inputs and reject accidental gold/corpus changes."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from docket.eval.schema import GoldSet, GoldSetError, fingerprint


class FrozenBenchmark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    gold_fingerprint: str
    files: dict[str, str]  # corpus-relative path -> SHA-256 of original bytes
    repeats: int = 3


def corpus_hashes(corpus: Path) -> dict[str, str]:
    if not corpus.is_dir():
        raise GoldSetError(f"corpus folder does not exist: {corpus}")
    result: dict[str, str] = {}
    for path in sorted(corpus.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".pdf", ".docx"}:
            with path.open("rb") as stream:
                result[path.relative_to(corpus).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    if not result:
        raise GoldSetError("cannot freeze a corpus with no PDF/DOCX files")
    return result


def freeze_benchmark(gold: GoldSet, corpus: Path, out: Path) -> FrozenBenchmark:
    tests = [q for q in gold.questions if q.split.value == "test"]
    if not tests or any(not q.reviewed for q in tests):
        raise GoldSetError("freeze requires a nonempty, reviewed test split")
    if any(q.answerable and not q.source_documents for q in tests):
        raise GoldSetError("answerable test questions need source_documents before freezing")
    files = corpus_hashes(corpus)
    for question in gold.questions:
        for document in question.source_documents:
            if document not in files:
                raise GoldSetError(f"{question.id}: source_documents path is not in the corpus: {document}")
    manifest = FrozenBenchmark(gold_fingerprint=fingerprint(gold), files=files)
    out.parent.mkdir(parents=True, exist_ok=True)
    # A freeze is immutable: create a new manifest after intentionally changing inputs.
    with out.open("x", encoding="utf-8") as stream:
        stream.write(manifest.model_dump_json(indent=2) + "\n")
    return manifest


def load_benchmark(path: Path) -> FrozenBenchmark:
    try:
        return FrozenBenchmark.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise GoldSetError(f"cannot read benchmark manifest {path}: {exc}") from exc


def verify_benchmark(manifest: FrozenBenchmark, gold: GoldSet, corpus: Path) -> str:
    if manifest.gold_fingerprint != fingerprint(gold):
        raise GoldSetError("gold set changed since this benchmark was frozen")
    if manifest.files != corpus_hashes(corpus):
        raise GoldSetError("corpus files changed since this benchmark was frozen")
    return fingerprint(manifest)
