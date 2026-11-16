"""Where an indexed file's original bytes live, and whether they still match.

Two storage modes coexist:

- copy (default): the original's bytes are in the content-addressed store, so
  the original is always available exactly as indexed.
- reference (``Settings.reference_mode``): only path, content hash, size and
  mtime are recorded; the bytes stay in the user's folder. The indexed text and
  chunks are kept either way, so a moved, deleted or edited file never loses
  what was indexed -- but it must never be presented as the indexed version.

``check_original`` answers "can I still read the indexed bytes, and are they the
same bytes?" cheaply (size, then mtime) and falls back to a streaming hash only
when the mtime moved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from docket.infra.evidence.store import ContentAddressedStore


class OriginalState(str, Enum):
    STORED = "stored"  # a private copy exists in the evidence store
    UNCHANGED = "unchanged"  # referenced file still has the indexed bytes
    CHANGED = "changed"  # referenced file exists but its content differs
    MISSING = "missing"  # referenced file moved, deleted or its folder is gone
    UNREADABLE = "unreadable"  # exists but cannot be read (permissions, I/O)


@dataclass(frozen=True)
class OriginalStatus:
    state: OriginalState
    path: Path | None
    detail: str

    @property
    def matches_indexed(self) -> bool:
        """True when the bytes behind this version are exactly the indexed ones."""
        return self.state in (OriginalState.STORED, OriginalState.UNCHANGED)

    @property
    def note(self) -> str:
        """Plain-language caveat to show next to a citation; empty when fine."""
        return "" if self.matches_indexed else self.detail


def check_original(
    store: ContentAddressedStore,
    *,
    content_hash: str,
    file_path: str | None,
    byte_size: int | None,
    source_mtime: float | None,
    verify_hash: bool = True,
) -> OriginalStatus:
    if store.exists(content_hash):
        return OriginalStatus(OriginalState.STORED, store._object_path(content_hash), "")
    if not file_path:
        return OriginalStatus(
            OriginalState.MISSING, None, "The original file is not stored and its location was not recorded."
        )
    path = Path(file_path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return OriginalStatus(
            OriginalState.MISSING,
            path,
            f"The original file is no longer at {path}. The indexed text is kept, "
            "but the file may have been moved or deleted.",
        )
    except OSError as exc:
        return OriginalStatus(OriginalState.UNREADABLE, path, f"The original file cannot be read: {exc.strerror or exc}.")
    changed = OriginalStatus(
        OriginalState.CHANGED,
        path,
        "This file has changed since it was indexed. The passage shown is from the "
        "indexed version, not the file as it is now.",
    )
    if byte_size is not None and st.st_size != byte_size:
        return changed
    if source_mtime is not None and st.st_mtime == source_mtime:
        return OriginalStatus(OriginalState.UNCHANGED, path, "")
    if not verify_hash:
        return changed
    try:
        digest, _ = ContentAddressedStore.hash_file(path)
    except OSError as exc:
        return OriginalStatus(OriginalState.UNREADABLE, path, f"The original file cannot be read: {exc.strerror or exc}.")
    if digest == content_hash:
        return OriginalStatus(OriginalState.UNCHANGED, path, "")
    return changed


def read_original_bytes(
    store: ContentAddressedStore,
    *,
    content_hash: str,
    file_path: str | None,
    byte_size: int | None,
    source_mtime: float | None,
) -> bytes | None:
    """The indexed bytes, from the store or an unchanged referenced file; None
    when they are not available (never returns bytes that differ from the index)."""
    if not store.exists(content_hash) and not store.is_reference(content_hash):
        return None  # a stored copy that went missing is damage, not a reference
    status = check_original(
        store, content_hash=content_hash, file_path=file_path, byte_size=byte_size, source_mtime=source_mtime
    )
    if not status.matches_indexed or status.path is None:
        return None
    data = status.path.read_bytes()
    if status.state is OriginalState.UNCHANGED and ContentAddressedStore._hash_bytes(data) != content_hash:
        return None  # changed between the check and the read
    return data


def stat_signature(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns
