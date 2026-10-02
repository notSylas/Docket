"""Content-addressed, filesystem-backed blob store for evidence bytes.

Layout, rooted at ``settings.evidence_store_path``:

- ``objects/<hash[:2]>/<hash>``            raw stored bytes, keyed by sha256 hex digest
- ``manifests/<hash[:2]>/<hash>.json``     JSON sidecar metadata for an object
- ``quarantine/``                          RESERVED for future use (e.g. malformed
                                            or unparseable files pending review) --
                                            no active logic in this checkpoint.
- ``trash/``                               Soft-delete staging: ``move_to_trash()``
                                            atomically moves a zero-referenced object
                                            here (flat, keyed by hash, no sharding);
                                            ``sweep_trash()`` permanently deletes an
                                            entry only after its own independent grace
                                            period has elapsed (Upgrade doc 03 section 8).

Objects and manifests are sharded into two-level directories keyed by the
first two hex characters of the hash (``<hash[:2]>/<hash>``) so that a large
evidence store doesn't end up with one giant flat directory.

SECURITY NOTE -- encryption at rest is NOT implemented in this checkpoint.
Bytes are written to disk as plain, unencrypted files. This is a deliberate
scope cut for CP2, not an oversight: encryption-at-rest is deferred to a
later milestone. ``ENCRYPTION_ENABLED`` below exists so this fact is visible
in code (and greppable) rather than silently absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Deferred TODO (later milestone): encrypt objects/manifests at rest (e.g. via
# an OS keychain-backed key + AES-GCM per object). Not implemented in CP2.
ENCRYPTION_ENABLED = False


class ObjectNotFoundError(Exception):
    """Raised when a requested content_hash is not present in the store."""

    def __init__(self, content_hash: str) -> None:
        self.content_hash = content_hash
        super().__init__(f"object not found in evidence store: {content_hash}")


class ContentAddressedStore:
    """A sha256 content-addressed blob store rooted at a directory tree.

    Assumes ``root`` already has ``objects/``, ``manifests/``, ``quarantine/``,
    and ``trash/`` subdirectories (see ``Settings.ensure_data_dirs()``).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.manifests_dir = self.root / "manifests"
        self.trash_dir = self.root / "trash"

    # -- paths ----------------------------------------------------------

    def _object_path(self, content_hash: str) -> Path:
        return self.objects_dir / content_hash[:2] / content_hash

    def _manifest_path(self, content_hash: str) -> Path:
        return self.manifests_dir / content_hash[:2] / f"{content_hash}.json"

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # -- atomic write helper ---------------------------------------------

    def _atomic_write(self, final_path: Path, data: bytes) -> None:
        """Write ``data`` to ``final_path`` atomically.

        Writes to a temp file in the same directory as ``final_path`` (so the
        rename is on the same filesystem, making it atomic on POSIX), then
        ``os.replace``s it into place. A crash mid-write leaves at most a
        stray ``.tmp-*`` file, never a corrupt/partial file visible under the
        real hash path.
        """
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.parent / f".tmp-{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, final_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- public API -------------------------------------------------------

    def put(self, data: bytes) -> str:
        """Store ``data``, returning its sha256 hex content_hash.

        If an object with this hash already exists, this is a no-op: the
        content is already stored, so we don't rewrite it.
        """
        content_hash = self._hash_bytes(data)
        object_path = self._object_path(content_hash)
        if object_path.exists():
            return content_hash
        self._atomic_write(object_path, data)
        return content_hash

    def exists(self, content_hash: str) -> bool:
        return self._object_path(content_hash).exists()

    def get(self, content_hash: str) -> bytes:
        object_path = self._object_path(content_hash)
        if not object_path.exists():
            raise ObjectNotFoundError(content_hash)
        return object_path.read_bytes()

    def write_manifest(self, content_hash: str, manifest: dict) -> None:
        manifest_path = self._manifest_path(content_hash)
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write(manifest_path, payload)

    def read_manifest(self, content_hash: str) -> dict:
        manifest_path = self._manifest_path(content_hash)
        if not manifest_path.exists():
            raise ObjectNotFoundError(content_hash)
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    # -- two-phase delete (Upgrade doc 03 section 8) ----------------------

    def _trash_path(self, content_hash: str) -> Path:
        return self.trash_dir / content_hash

    def move_to_trash(self, content_hash: str) -> Path | None:
        """Phase 1 of safe blob deletion: atomically move a (caller-verified
        zero-referenced) object from `objects/<hash[:2]>/<hash>` to
        `trash/<hash>`, the same `os.replace` atomic-rename pattern
        `_atomic_write` already uses -- never an immediate unlink.

        Stamps the moved file's mtime to "now" right after the move, since
        `os.replace` preserves the original file's mtime (which, for a
        globally-deduped object, could be from long before it became
        unreferenced). `sweep_trash`'s grace window is measured from *this*
        timestamp -- "time spent in trash/", not the blob's original
        creation time.

        A no-op (returns `None`) if the object doesn't exist in `objects/`
        -- already trashed, or never existed -- so a caller can call this
        defensively without first checking existence. Does not touch the
        manifest: the manifest is only removed once `sweep_trash` actually,
        permanently deletes the trashed object.
        """
        object_path = self._object_path(content_hash)
        if not object_path.exists():
            return None
        trash_path = self._trash_path(content_hash)
        trash_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(object_path, trash_path)
        now = time.time()
        os.utime(trash_path, (now, now))
        return trash_path

    def is_in_trash(self, content_hash: str) -> bool:
        return self._trash_path(content_hash).exists()

    def sweep_trash(
        self, grace_period: timedelta, *, now: datetime | None = None
    ) -> list[str]:
        """Phase 2 of safe blob deletion, run independently of (and later
        than) `move_to_trash`: permanently delete every object under
        `trash/` that has sat there for at least `grace_period`, judged by
        the mtime `move_to_trash` stamped on it.

        Deliberately a separate, independently-callable sweep rather than
        something `move_to_trash` triggers itself -- the trash grace window
        is independent of (and, by default, much shorter than) the
        TOMBSTONED retention window in `SourceManager`: this window is only
        a last-resort safety margin against a buggy purge job, not a chance
        for a deleted source to reappear (Upgrade doc 03 section 8).

        Also removes the now-permanently-deleted object's manifest, if one
        exists -- a manifest for a hash that no longer exists anywhere is
        dead metadata, not a resource to preserve. Returns the list of
        content_hashes permanently deleted.
        """
        now = now if now is not None else datetime.now(timezone.utc)
        cutoff = (now - grace_period).timestamp()

        deleted: list[str] = []
        if not self.trash_dir.exists():
            return deleted

        for path in self.trash_dir.iterdir():
            if not path.is_file():
                continue
            if path.stat().st_mtime <= cutoff:
                content_hash = path.name
                path.unlink(missing_ok=True)
                self._manifest_path(content_hash).unlink(missing_ok=True)
                deleted.append(content_hash)
        return deleted
