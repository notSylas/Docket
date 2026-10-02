"""Tests for ContentAddressedStore -- pure filesystem, no DB."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from docket.infra.evidence.store import ContentAddressedStore, ObjectNotFoundError


@pytest.fixture()
def store(tmp_path: Path) -> ContentAddressedStore:
    for sub in ("objects", "manifests", "quarantine", "trash"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return ContentAddressedStore(tmp_path)


def test_put_get_round_trip(store: ContentAddressedStore) -> None:
    data = b"hello evidence store"
    content_hash = store.put(data)

    assert content_hash == hashlib.sha256(data).hexdigest()
    assert store.get(content_hash) == data


def test_put_is_idempotent_no_op_on_duplicate(store: ContentAddressedStore) -> None:
    data = b"duplicate content"

    hash1 = store.put(data)
    object_path = store._object_path(hash1)
    mtime_after_first = object_path.stat().st_mtime_ns

    # Sanity: force a detectable gap so a rewrite would be observable.
    import time

    time.sleep(0.01)

    hash2 = store.put(data)
    mtime_after_second = object_path.stat().st_mtime_ns

    assert hash1 == hash2
    assert mtime_after_first == mtime_after_second, "second put() rewrote the object"


def test_get_unknown_hash_raises(store: ContentAddressedStore) -> None:
    with pytest.raises(ObjectNotFoundError):
        store.get("f" * 64)


def test_exists(store: ContentAddressedStore) -> None:
    data = b"exists check"
    assert not store.exists(hashlib.sha256(data).hexdigest())
    content_hash = store.put(data)
    assert store.exists(content_hash)


def test_manifest_write_read_round_trip(store: ContentAddressedStore) -> None:
    data = b"some file bytes"
    content_hash = store.put(data)
    manifest = {
        "byte_size": len(data),
        "mime_type": "text/plain",
        "original_filename": "notes.txt",
    }
    store.write_manifest(content_hash, manifest)

    assert store.read_manifest(content_hash) == manifest


def test_read_manifest_unknown_hash_raises(store: ContentAddressedStore) -> None:
    with pytest.raises(ObjectNotFoundError):
        store.read_manifest("a" * 64)


def test_no_leftover_tmp_files_after_put(store: ContentAddressedStore) -> None:
    store.put(b"clean write one")
    store.put(b"clean write two")
    store.write_manifest(
        hashlib.sha256(b"clean write one").hexdigest(),
        {"byte_size": 16, "mime_type": None, "original_filename": "a.bin"},
    )

    leftover_tmp_objects = list(store.objects_dir.rglob(".tmp-*"))
    leftover_tmp_manifests = list(store.manifests_dir.rglob(".tmp-*"))

    assert leftover_tmp_objects == []
    assert leftover_tmp_manifests == []


def test_sharded_directory_layout(store: ContentAddressedStore) -> None:
    data = b"sharding check"
    content_hash = store.put(data)
    expected_path = store.objects_dir / content_hash[:2] / content_hash
    assert expected_path.exists()
    assert expected_path.is_file()


# ---------------------------------------------------------------------------
# Two-phase delete (Upgrade doc 03 section 8): move_to_trash / sweep_trash.
# ---------------------------------------------------------------------------


def test_move_to_trash_relocates_object_out_of_objects_dir(store: ContentAddressedStore) -> None:
    data = b"doomed bytes"
    content_hash = store.put(data)

    trash_path = store.move_to_trash(content_hash)

    assert trash_path == store.trash_dir / content_hash
    assert trash_path.exists()
    assert trash_path.read_bytes() == data
    assert not store._object_path(content_hash).exists()
    assert not store.exists(content_hash)
    assert store.is_in_trash(content_hash)


def test_move_to_trash_is_a_no_op_for_unknown_hash(store: ContentAddressedStore) -> None:
    result = store.move_to_trash("f" * 64)

    assert result is None
    assert not store.is_in_trash("f" * 64)


def test_move_to_trash_does_not_delete_manifest(store: ContentAddressedStore) -> None:
    """The manifest is only cleaned up once `sweep_trash` permanently
    deletes the blob -- not at the move-to-trash phase (doc 03 section 8
    only specifies moving the object itself)."""
    data = b"has a manifest"
    content_hash = store.put(data)
    store.write_manifest(content_hash, {"byte_size": len(data), "mime_type": None, "original_filename": "x"})

    store.move_to_trash(content_hash)

    assert store.read_manifest(content_hash) is not None


def test_sweep_trash_deletes_only_entries_past_grace_period(
    store: ContentAddressedStore,
) -> None:
    old_hash = store.put(b"old trashed content")
    new_hash = store.put(b"freshly trashed content")
    store.move_to_trash(old_hash)
    store.move_to_trash(new_hash)

    # Back-date only `old_hash`'s trash entry to look like it's been sitting
    # there for 10 days.
    old_trash_path = store.trash_dir / old_hash
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(old_trash_path, (ten_days_ago, ten_days_ago))

    deleted = store.sweep_trash(timedelta(days=7))

    assert deleted == [old_hash]
    assert not old_trash_path.exists()
    assert (store.trash_dir / new_hash).exists()


def test_sweep_trash_also_removes_the_manifest(store: ContentAddressedStore) -> None:
    data = b"manifest cleanup check"
    content_hash = store.put(data)
    store.write_manifest(
        content_hash, {"byte_size": len(data), "mime_type": None, "original_filename": "x"}
    )
    store.move_to_trash(content_hash)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(store.trash_dir / content_hash, (old, old))

    store.sweep_trash(timedelta(days=7))

    with pytest.raises(ObjectNotFoundError):
        store.read_manifest(content_hash)


def test_sweep_trash_leaves_entries_within_grace_period_untouched(
    store: ContentAddressedStore,
) -> None:
    content_hash = store.put(b"too fresh to sweep")
    store.move_to_trash(content_hash)

    deleted = store.sweep_trash(timedelta(days=7))

    assert deleted == []
    assert (store.trash_dir / content_hash).exists()


def test_sweep_trash_is_independent_of_the_30_day_retention_window(
    store: ContentAddressedStore,
) -> None:
    """The trash grace window (default 7 days) is independent of, and
    shorter than, the 30-day TOMBSTONED retention window -- a blob trashed
    10 days ago must already be swept even though 10 < 30 (doc 03 section
    8's explicit decision that the two windows must not be conflated)."""
    content_hash = store.put(b"independent windows check")
    store.move_to_trash(content_hash)
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(store.trash_dir / content_hash, (ten_days_ago, ten_days_ago))

    deleted = store.sweep_trash(timedelta(days=7))

    assert deleted == [content_hash]


def test_sweep_trash_respects_explicit_now(store: ContentAddressedStore) -> None:
    content_hash = store.put(b"explicit now check")
    store.move_to_trash(content_hash)

    # "Now" 3 days in the future with a 7-day grace period: not expired yet.
    not_yet = store.sweep_trash(
        timedelta(days=7), now=datetime.now(timezone.utc) + timedelta(days=3)
    )
    assert not_yet == []

    # "Now" 8 days in the future: expired.
    expired = store.sweep_trash(
        timedelta(days=7), now=datetime.now(timezone.utc) + timedelta(days=8)
    )
    assert expired == [content_hash]


def test_sweep_trash_on_empty_trash_dir_returns_empty_list(
    store: ContentAddressedStore,
) -> None:
    assert store.sweep_trash(timedelta(days=7)) == []
