"""Builds the runtime dependency graph (engine, session_factory, store,
gateway, index writers, parser, pipeline, ...) for the CLI, from
`attest.config.Settings`.

Two deliberate choices:

1. `AppContext.__init__` constructs its own fresh `Settings()` instance
   instead of importing the process-wide `attest.config.settings` singleton.
   The singleton is built once, at first import of `attest.config` -- if
   that happens to occur before a caller sets `ATTEST_DATA_DIR` (e.g. a test
   harness that `monkeypatch.setenv`s it right before invoking the CLI, or
   simply because some other module imported `attest.config` earlier in the
   process), the singleton would silently keep pointing at the default data
   dir. Building `Settings()` fresh here means every CLI invocation re-reads
   the environment, so `ATTEST_DATA_DIR` always takes effect regardless of
   import order.
2. Everything heavier than "read settings and open a SQLite engine" is
   built lazily (`functools.cached_property`) and only on first access.
   `DoclingParser()` in particular loads layout/OCR/table models on
   construction (multi-second cost) -- commands that never touch parsing
   (`sources add`, `sources list`) must not pay for it.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from attest.config import Settings
from attest.db.engine import get_engine, get_session_factory
from attest.evidence.manager import EvidenceManager
from attest.evidence.store import ContentAddressedStore
from attest.index.fts_index import FtsIndexWriter
from attest.index.manager import IndexManager
from attest.index.vector_index import LanceIndexWriter
from attest.inference.gateway import OllamaGateway
from attest.ingestion.pipeline import IngestionPipeline
from attest.parsing.docling_wrapper import DoclingParser
from attest.parsing.recipes import DEFAULT_SPLITTER, ChunkRecipe
from attest.retrieval.resolver import EvidenceResolver
from attest.sources.manager import SourceManager

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config(sqlite_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "src" / "attest" / "db" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return config


def _ensure_schema(sqlite_path: Path) -> None:
    """Run Alembic migrations up to `head` if the DB file doesn't exist yet,
    so a fresh CLI run against an empty data dir never crashes with "no such
    table". If the file already exists, it's assumed to already be migrated
    (or managed manually via `alembic upgrade head`) -- we don't re-run
    migrations on every single CLI invocation against an existing DB."""
    if sqlite_path.exists():
        return
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
        )


def build_context() -> AppContext:
    return AppContext()
