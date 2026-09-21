"""Content-addressed, filesystem-backed blob store for evidence bytes.

Layout, rooted at ``settings.evidence_store_path``:

- ``objects/<hash[:2]>/<hash>``            raw stored bytes, keyed by sha256 hex digest
- ``manifests/<hash[:2]>/<hash>.json``     JSON sidecar metadata for an object
- ``quarantine/``                          RESERVED for future use (e.g. malformed
                                            or unparseable files pending review) --
                                            no active logic in this checkpoint.
- ``trash/``                               RESERVED for future use (e.g. soft-delete
                                            staging before a hard purge) -- no active
                                            logic in this checkpoint.

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
import uuid
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
