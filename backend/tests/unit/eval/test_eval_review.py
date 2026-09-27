"""Unit tests for the shared human-labeling review harness
(`docket.eval.review`) that `eval.calibration` and `eval.formula_review`
are both built on top of."""

from __future__ import annotations

import pytest
import yaml

from docket.eval.review import DEFAULT_SAMPLE_SIZE, ReviewResult, bucket_sample, load_labels_yaml


class _ReviewError(ValueError):
    """A stand-in for each caller's own exception type."""


def test_default_sample_size_is_thirty():
    assert DEFAULT_SAMPLE_SIZE == 30


def test_review_result_holds_the_shared_labeled_unlabeled_agreement_shape():
    result = ReviewResult(labeled=3, unlabeled=1, agreement=0.5)
    assert (result.labeled, result.unlabeled, result.agreement) == (3, 1, 0.5)


# ---------------------------------------------------------------------------
# bucket_sample
# ---------------------------------------------------------------------------


def test_bucket_sample_caps_at_total_available_items():
    items = [("a", 0), ("a", 1), ("b", 0)]
    picked = bucket_sample(items, 100, bucket_key=lambda x: x[0], sort_key=lambda x: x)
    assert len(picked) == 3
    assert sorted(picked) == sorted(items)


def test_bucket_sample_spreads_round_robin_across_every_bucket():
    # 5 items in bucket "a", 1 in "b": a plain random sample of 2 could
    # easily land entirely in "a"; the round-robin must not.
    items = [("a", i) for i in range(5)] + [("b", 0)]
    picked = bucket_sample(items, 2, bucket_key=lambda x: x[0], sort_key=lambda x: x, seed=0)
    assert len(picked) == 2
    assert {p[0] for p in picked} == {"a", "b"}


def test_bucket_sample_order_key_controls_which_bucket_goes_first():
    # Single-item buckets so round-robin order is externally observable in
    # the output order: with n == number of buckets, lap order == order_key.
    items = [("z", 0), ("a", 0), ("m", 0)]
    picked = bucket_sample(
        items, 3, bucket_key=lambda x: x[0], sort_key=lambda x: x, order_key=lambda k: k
    )
    assert [p[0] for p in picked] == ["a", "m", "z"]


def test_bucket_sample_is_deterministic_given_the_same_seed():
    items = [("a", i) for i in range(6)] + [("b", i) for i in range(6)]
    first = bucket_sample(items, 4, bucket_key=lambda x: x[0], sort_key=lambda x: x, seed=7)
    second = bucket_sample(items, 4, bucket_key=lambda x: x[0], sort_key=lambda x: x, seed=7)
    assert first == second


def test_bucket_sample_distinct_key_avoids_repeats_while_options_remain():
    # No overlap between the two buckets' distinct-key values: a single,
    # repeat-free pass must be able to satisfy n=4 by taking one of each.
    items = [("a", "q1"), ("a", "q2"), ("b", "q3"), ("b", "q4")]
    picked = bucket_sample(
        items, 4, bucket_key=lambda x: x[0], sort_key=lambda x: x, distinct_key=lambda x: x[1]
    )
    assert sorted(p[1] for p in picked) == ["q1", "q2", "q3", "q4"]


def test_bucket_sample_distinct_key_falls_back_to_repeats_to_reach_n():
    # Every item in the only bucket shares the same distinct_key; n=2 is
    # only reachable at all by allowing a repeat in a second pass.
    items = [("a", "q1"), ("a", "q1")]
    picked = bucket_sample(
        items, 2, bucket_key=lambda x: x[0], sort_key=lambda x: x, distinct_key=lambda x: x[1]
    )
    assert len(picked) == 2


# ---------------------------------------------------------------------------
# load_labels_yaml
# ---------------------------------------------------------------------------


def test_load_labels_yaml_reads_valid_items(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"items": [{"id": "a", "correct": True}, {"id": "b", "correct": None}]}))
    items = load_labels_yaml(path, error_cls=_ReviewError)
    assert [i["id"] for i in items] == ["a", "b"]


def test_load_labels_yaml_rejects_missing_items_list(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"nope": []}))
    with pytest.raises(_ReviewError, match="items"):
        load_labels_yaml(path, error_cls=_ReviewError)


def test_load_labels_yaml_rejects_missing_id(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"items": [{"correct": True}]}))
    with pytest.raises(_ReviewError, match="needs an id"):
        load_labels_yaml(path, error_cls=_ReviewError)


def test_load_labels_yaml_rejects_duplicate_ids_with_custom_label(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"items": [{"id": "a"}, {"id": "a"}]}))
    with pytest.raises(_ReviewError, match="duplicate widget label: a"):
        load_labels_yaml(path, error_cls=_ReviewError, duplicate_label="widget label")


def test_load_labels_yaml_rejects_non_boolean_correct(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"items": [{"id": "a", "correct": "maybe"}]}))
    with pytest.raises(_ReviewError, match="true or false"):
        load_labels_yaml(path, error_cls=_ReviewError)


def test_load_labels_yaml_rejects_unreadable_file(tmp_path):
    with pytest.raises(_ReviewError, match="cannot read labels file"):
        load_labels_yaml(tmp_path / "missing.yaml", error_cls=_ReviewError)


def test_load_labels_yaml_runs_the_validate_item_callback_per_item(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"items": [{"id": "a"}, {"id": "b"}]}))

    seen = []

    def _validate(item):
        seen.append(item["id"])
        if item["id"] == "b":
            raise _ReviewError("b is bad")

    with pytest.raises(_ReviewError, match="b is bad"):
        load_labels_yaml(path, error_cls=_ReviewError, validate_item=_validate)
    assert seen == ["a", "b"]
