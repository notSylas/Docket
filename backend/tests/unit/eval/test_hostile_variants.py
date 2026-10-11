"""Additive fixture validation only: no model calls or eval runs."""
import re
from pathlib import Path

import pytest

from docket.eval.schema import QuestionType, load_gold_set
from docket.eval.benchmark import freeze_benchmark
from docket.infra.parsing.text_family import TextFamilyParser, chunk_text

PUBLIC = Path(__file__).resolve().parents[3] / "eval-public"
CORPUS = PUBLIC / "corpus-synthetic-g4-hostile"
GOLD = PUBLIC / "gold-g4-hostile.yaml"


def test_hostile_gold_uses_existing_schema_and_canary_contracts():
    gold = load_gold_set(GOLD)
    assert gold.population == "synthetic_public"
    assert len(gold.questions) == 7
    for question in gold.questions:
        assert question.reviewed and question.split.value == "test"
        assert any("CANARY_" in pattern for pattern in question.must_not_contain)
        assert r"re:\bcanary_[a-z0-9_]+\b" in question.must_not_contain
    revoked = next(q for q in gold.questions if q.type == QuestionType.REVOKED)
    assert revoked.setup.revoke == ["restricted"] and not revoked.answerable


def test_variant_corpus_can_be_frozen_with_existing_manifest_schema(tmp_path):
    frozen = freeze_benchmark(load_gold_set(GOLD), CORPUS, tmp_path / "frozen.json")
    assert set(frozen.files) == {
        p.relative_to(CORPUS).as_posix() for p in CORPUS.rglob("*") if p.is_file()}


@pytest.mark.parametrize("question", load_gold_set(GOLD).questions, ids=lambda q: q.id)
def test_every_gold_span_is_verbatim_in_original_and_stored_chunks(question):
    originals, extracted = [], []
    for name in question.source_documents:
        path = CORPUS / name
        assert path.is_file() and CORPUS in path.resolve().parents
        originals.append(path.read_text(encoding="utf-8"))
        parsed = TextFamilyParser().parse("fixture", path)
        _, chunks = chunk_text(parsed)
        extracted.extend(c.text for c in chunks)
    for span in question.gold_spans:
        assert any(span in original for original in originals)
        assert any(span in chunk for chunk in extracted)


def test_hidden_attacks_removed_but_visible_attack_variants_remain_data():
    for name, canary in [("html-comment.html", "CANARY_HTML_COMMENT_7F39"),
                         ("alt-text.html", "CANARY_ALT_TEXT_4D63")]:
        path = CORPUS / "public" / name
        assert canary in path.read_text()
        text = " ".join(b.text for b in TextFamilyParser().parse("fixture", path).blocks)
        assert canary not in text
        assert "CANARY_ACTIVE_LINK_5E74" not in text
    for name in ("csv-cell.csv", "email-headers.txt", "ocr-noise.txt"):
        path = CORPUS / "public" / name
        canaries = re.findall(r"CANARY_[A-Z0-9_]+", path.read_text())
        text = " ".join(b.text for b in TextFamilyParser().parse("fixture", path).blocks)
        assert canaries and all(c in text for c in canaries)
