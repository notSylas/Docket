import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from docket.eval.schema import load_gold_set
from docket.services.feedback import FeedbackError, FeedbackStore


QUOTE = "The committee approved the revised budget."


@pytest.fixture
def resolver():
    ev = SimpleNamespace(chunk_id="chunk-1", evidence_version_id="version-1",
                         text=QUOTE, source_display_name="minutes.txt")
    return SimpleNamespace(resolve=lambda chunk_id: ev)


def capture(store, **kwargs):
    return store.capture("What was approved?", "The budget was approved. [1]",
                         [{"chunk_id": "chunk-1", "evidence_version_id": "version-1"}],
                         "good", **kwargs)


def test_feedback_is_off_by_default_and_creates_nothing(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(FeedbackError, match="off"):
        capture(FeedbackStore(root))
    assert not root.exists()


def test_capture_is_private_durable_keeps_versions_and_never_logs(tmp_path, caplog):
    store = FeedbackStore(tmp_path, True)
    with caplog.at_level(logging.DEBUG):
        item_id = capture(store, correction="PRIVATE correction\nsecond line")
    assert not caplog.records
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    item = FeedbackStore(tmp_path).items()[0]
    assert item["id"] == item_id and item["label"] == "good"
    assert item["question"] == "What was approved?" and "[1]" in item["answer"]
    assert item["citations"] == [{"chunk_id": "chunk-1", "evidence_version_id": "version-1"}]
    assert item["correction"] == "PRIVATE correction second line" and item["reviewed"] is False


def test_only_reviewed_items_export_and_all_exported_questions_remain_drafts(tmp_path, resolver):
    store = FeedbackStore(tmp_path, True)
    first, second = capture(store), capture(store)
    store.review(first, resolver, expected="budget", spans=[QUOTE])
    raw, skipped = store.export_gold(resolver)
    path = tmp_path / "DRAFT.yml"; path.write_text(raw)
    gold = load_gold_set(path)
    assert len(gold.questions) == 1 and skipped == 0
    q = gold.questions[0]
    assert q.id == "feedback-" + first and second not in raw
    assert q.reviewed is False and q.origin.startswith("DRAFT")
    assert q.gold_spans == [QUOTE] and q.must_contain == ["budget"]
    assert "correction" not in raw and "answer:" not in raw
    assert len(store.path.read_text().splitlines()) == 3  # append-only review


def test_review_requires_verbatim_span_and_matching_saved_version(tmp_path, resolver):
    store = FeedbackStore(tmp_path, True)
    item = capture(store)
    with pytest.raises(FeedbackError, match="verbatim"):
        store.review(item, resolver, expected="budget", spans=["This is not the original evidence quote."])
    changed = SimpleNamespace(resolve=lambda cid: SimpleNamespace(evidence_version_id="new-version"))
    with pytest.raises(FeedbackError, match="changed"):
        store.review(item, changed, expected="budget", spans=[QUOTE])
    assert store.items()[0]["reviewed"] is False


def test_export_drops_unavailable_reviewed_evidence_and_rejects_bad_review(tmp_path, resolver):
    store = FeedbackStore(tmp_path, True)
    item = capture(store)
    with pytest.raises(FeedbackError, match="expected fact"):
        store.review(item, resolver)
    store.review(item, resolver, expected="budget", spans=[QUOTE])
    def missing(cid): raise ValueError("SECRET details")
    output, skipped = store.export_gold(SimpleNamespace(resolve=missing))
    assert skipped == 1 and "questions: []" in output and "SECRET" not in output


def test_partial_final_append_keeps_prior_items_and_symlinks_are_refused(tmp_path):
    store = FeedbackStore(tmp_path / "data", True)
    capture(store)
    with store.path.open("ab") as stream: stream.write(b'{"unfinished":')
    assert len(store.items()) == 1
    capture(store)
    assert len(store.items()) == 2
    other = tmp_path / "outside"; other.mkdir()
    linked = tmp_path / "linked"; linked.mkdir()
    (linked / "feedback").symlink_to(other, target_is_directory=True)
    with pytest.raises(FeedbackError, match="symbolic"):
        capture(FeedbackStore(linked, True))
    assert list(other.iterdir()) == []
