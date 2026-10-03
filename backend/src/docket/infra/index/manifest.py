"""Index manifest -- records which embedding space the vector indexes live in.

A small JSON file (`Settings.index_manifest_path`, next to the LanceDB
directory) holding `schema_version`, `embed_model`, `embed_dimension`,
`embed_instruction` and `tokenizer` (both null for now) and
`created_at`/`updated_at`. It is written atomically (temp file +
`os.replace`).

Policy, enforced by `IndexManifestGuard`:

- Never mix vector spaces. Before embeddings are written (to `chunks` or
  `pages`), the configured embed model and the *actual* embedding dimension
  must equal the manifest's; otherwise `IndexManifestMismatchError` is
  raised, nothing is written, and the message says to run `docket reindex`.
- No manifest and no existing vector tables: the first write creates it.
- Legacy install (vector tables exist, no manifest): adopted -- the current
  model is recorded -- only when every existing table's vector dimension
  equals the current model's actual embedding dimension. Dimension is the
  only thing a legacy table can prove, so a same-dimension model swap made
  before the manifest existed cannot be detected; a different dimension is
  refused.
- Query side: when a manifest exists, searching with a different model or
  dimension is refused instead of returning rankings from the wrong space.
  With no manifest the check is skipped (nothing is known to compare to).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

SCHEMA_VERSION = 1


class IndexManifestMismatchError(Exception):
    """The configured embedding model/dimension disagrees with the index
    manifest (or with a legacy table's vector dimension)."""


@dataclass(frozen=True)
class IndexManifest:
    schema_version: int
    embed_model: str
    embed_dimension: int
    embed_instruction: str | None
    tokenizer: str | None
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_manifest(
    embed_model: str, embed_dimension: int, *, created_at: str | None = None
) -> IndexManifest:
    now = _now()
    return IndexManifest(
        schema_version=SCHEMA_VERSION,
        embed_model=embed_model,
        embed_dimension=embed_dimension,
        embed_instruction=None,
        tokenizer=None,
        created_at=created_at or now,
        updated_at=now,
    )


def read_manifest(path: Path) -> IndexManifest | None:
    """The manifest at `path`, or `None` if there is none."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return IndexManifest(**json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise IndexManifestMismatchError(
            f"Index manifest {path} is unreadable ({exc}); fix or remove it, "
            "then run `docket reindex`."
        ) from exc


def write_manifest(path: Path, manifest: IndexManifest) -> None:
    """Write atomically: a crash leaves the old manifest (or a stray
    `.tmp-*` file), never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(manifest), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def check_compatible(manifest: IndexManifest, embed_model: str, dimension: int) -> None:
    """Raise `IndexManifestMismatchError` unless model and dimension match."""
    if manifest.embed_model != embed_model:
        raise IndexManifestMismatchError(
            f"The index was built with embedding model {manifest.embed_model!r} but the "
            f"configured model is {embed_model!r}. Run `docket reindex` to rebuild it."
        )
    if manifest.embed_dimension != dimension:
        raise IndexManifestMismatchError(
            f"The index holds {manifest.embed_dimension}-dimensional vectors but the "
            f"embedding model produced {dimension}. Run `docket reindex` to rebuild it."
        )


class IndexManifestGuard:
    """Applies the policy in the module docstring for one install.

    `table_dimensions` returns the vector dimension of each existing vector
    table (`None` for a table that doesn't exist yet); it is only consulted
    when there is no manifest.
    """

    def __init__(
        self,
        path: Path,
        embed_model: str,
        table_dimensions: Callable[[], Iterable[int | None]] = lambda: (),
    ):
        self._path = path
        self._embed_model = embed_model
        self._table_dimensions = table_dimensions

    def check_write(self, dimension: int) -> None:
        """Call with the actual embedding dimension before writing vectors."""
        manifest = read_manifest(self._path)
        if manifest is not None:
            check_compatible(manifest, self._embed_model, dimension)
            return
        for existing in self._table_dimensions():
            if existing is not None and existing != dimension:
                raise IndexManifestMismatchError(
                    f"An existing vector table holds {existing}-dimensional vectors (no "
                    f"index manifest) but the embedding model produced {dimension}. "
                    "Run `docket reindex` to rebuild it."
                )
        write_manifest(self._path, new_manifest(self._embed_model, dimension))

    def check_query(self, dimension: int) -> None:
        """Call with the query vector's dimension before searching."""
        manifest = read_manifest(self._path)
        if manifest is not None:
            check_compatible(manifest, self._embed_model, dimension)
