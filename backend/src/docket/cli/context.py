"""Builds the runtime dependency graph (engine, session_factory, store,
gateway, index writers, parser, pipeline, ...) for the CLI, from
`docket.config.Settings`.

Two deliberate choices:

1. `AppContext.__init__` constructs its own fresh `Settings()` instance
   instead of importing the process-wide `docket.config.settings` singleton.
   The singleton is built once, at first import of `docket.config` -- if
   that happens to occur before a caller sets `DOCKET_DATA_DIR` (e.g. a test
   harness that `monkeypatch.setenv`s it right before invoking the CLI, or
   simply because some other module imported `docket.config` earlier in the
   process), the singleton would silently keep pointing at the default data
   dir. Building `Settings()` fresh here means every CLI invocation re-reads
   the environment, so `DOCKET_DATA_DIR` always takes effect regardless of
   import order.
2. Everything heavier than "read settings and open a SQLite engine" is
   built lazily (`functools.cached_property`) and only on first access.
   `DoclingParser()` in particular loads layout/OCR/table models on
   construction (multi-second cost) -- commands that never touch parsing
   (`sources add`, `sources list`) must not pay for it.
"""

from __future__ import annotations

import importlib.resources as resources
import sys
from functools import cached_property
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from docket.config import Settings
from docket.db.engine import get_engine, get_session_factory
from docket.evidence.manager import EvidenceManager
from docket.evidence.store import ContentAddressedStore
from docket.index.fts_index import FtsIndexWriter
from docket.index.manager import IndexManager
from docket.index.vector_index import LanceIndexWriter
from docket.index.visual_index import LancePageIndexWriter
from docket.inference.gateway import OllamaGateway
from docket.ingestion.pipeline import IngestionPipeline
from docket.parsing.docling_wrapper import DoclingParser
from docket.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from docket.retrieval.resolver import EvidenceResolver
from docket.sources.manager import SourceManager

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Running as a PyInstaller-frozen binary: `__file__`-based nesting no
    # longer matches the editable-install source layout below (the frozen
    # bundle has no `src/` prefix, so `parents[3]` would overshoot outside
    # the bundle entirely -- confirmed empirically during a de-risking
    # spike). `sys._MEIPASS` is the bundle's extraction root, and the
    # sidecar's `.spec` bundles `alembic.ini` and the migrations folder at
    # the same relative paths this module expects from REPO_ROOT in dev
    # (REPO_ROOT/alembic.ini, REPO_ROOT/src/docket/db/migrations) -- see
    # desktop/sidecar/app-sidecar-*.spec's `datas`.
    REPO_ROOT: Path | None = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    # Not a frozen build. `__file__`-based ancestor-climbing (e.g.
    # `parents[3]`) only ever pointed at a real project directory for an
    # *editable* dev install, where `__file__` resolves to
    # `backend/src/docket/cli/context.py` on disk. A normal `pip`/`pipx`
    # install builds a real wheel, which flattens `src/docket` into a
    # top-level `docket` package inside `site-packages` with no
    # `backend/`-equivalent ancestor at all -- there is nothing to climb to.
    # `importlib.resources` is the standard way to locate packaged data
    # (the `db/migrations` tree under this package) regardless of whether
    # the install is editable or a real wheel, so it replaces REPO_ROOT
    # entirely for this branch.
    REPO_ROOT = None


def _alembic_config(sqlite_path: Path) -> Config:
    if REPO_ROOT is not None:
        # Frozen (PyInstaller) case: alembic.ini/migrations are bundled at
        # sys._MEIPASS-relative paths by desktop/sidecar/app-sidecar-*.spec's
        # `datas` -- see that file.
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option(
            "script_location", str(REPO_ROOT / "src" / "docket" / "db" / "migrations")
        )
    else:
        # Editable or real pip/pipx install: importlib.resources finds the
        # packaged migrations directory regardless of install type, no
        # REPO_ROOT-relative alembic.ini file lookup needed. (backend/alembic.ini
        # still exists on disk for running `alembic` directly from a checkout
        # during schema development, but the CLI's own runtime path no longer
        # depends on finding it.)
        config = Config()
        migrations_dir = resources.files("docket.db") / "migrations"
        config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


def _ensure_schema(sqlite_path: Path) -> None:
    """Run Alembic migrations up to `head`, every CLI invocation.

    `alembic upgrade head` is a no-op (a fast read of the `alembic_version`
    table) when already current, so this is cheap for the common case and
    means a schema change (like 0003's FTS5 retokenization) actually reaches
    an existing user's DB on their next command, not just a fresh install.
    Previously this only ran when the DB file didn't exist yet, which left
    existing installs permanently on whatever schema they were created with
    unless someone ran `alembic upgrade head` by hand.
    """
    command.upgrade(_alembic_config(sqlite_path), "head")


class AppContext:
    """Holds the wired-up dependencies for one CLI invocation."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.settings.ensure_data_dirs()
        _ensure_schema(self.settings.sqlite_path)
        self.engine: Engine = get_engine(self.settings.sqlite_path)
        self.session_factory: sessionmaker = get_session_factory(self.engine)

    @cached_property
    def store(self) -> ContentAddressedStore:
        return ContentAddressedStore(self.settings.evidence_store_path)

    @cached_property
    def evidence_manager(self) -> EvidenceManager:
        return EvidenceManager(self.store, self.session_factory)

    @cached_property
    def parser(self) -> DoclingParser:
        return DoclingParser()

    @cached_property
    def gateway(self) -> OllamaGateway:
        return OllamaGateway()

    @cached_property
    def fts_writer(self) -> FtsIndexWriter:
        return FtsIndexWriter(self.engine)

    @cached_property
    def vector_writer(self) -> LanceIndexWriter:
        return LanceIndexWriter(self.settings.lancedb_path)

    @cached_property
    def visual_index_writer(self) -> LancePageIndexWriter:
        return LancePageIndexWriter(self.settings.lancedb_path)

    @cached_property
    def index_manager(self) -> IndexManager:
        return IndexManager(self.fts_writer, self.vector_writer, self.gateway)

    @cached_property
    def chunk_recipe(self) -> ChunkRecipe:
        return ChunkRecipe(
            chunk_size=self.settings.chunk_size_words,
            overlap=self.settings.chunk_overlap_words,
            splitter=DEFAULT_SPLITTER,
            parser_name=self.parser.parser_name,
            parser_version=self.parser.parser_version,
        )

    @cached_property
    def source_manager(self) -> SourceManager:
        return SourceManager(self.session_factory)

    @cached_property
    def resolver(self) -> EvidenceResolver:
        return EvidenceResolver(self.session_factory)

    @cached_property
    def pipeline(self) -> IngestionPipeline:
        return IngestionPipeline(
            session_factory=self.session_factory,
            evidence_manager=self.evidence_manager,
            parser=self.parser,
            index_manager=self.index_manager,
            chunk_recipe=self.chunk_recipe,
            gateway=self.gateway,
            visual_index_writer=self.visual_index_writer,
            settings=self.settings,
        )


def build_context() -> AppContext:
    return AppContext()
