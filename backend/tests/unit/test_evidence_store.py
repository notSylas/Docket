"""Tests for ContentAddressedStore -- pure filesystem, no DB."""

from __future__ import annotations

import hashlib
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
