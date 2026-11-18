"""Ingest a folder of your own documents into a SCRATCH Docket data dir and build the KAG entity graph.

    python ingest_folder.py DOCS_FOLDER SCRATCH_DIR

Your documents are only read, never modified. Nothing is written to ~/.local/share/docket (the script
refuses that path). Prints counts only, never document text or file names.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kit_common import EMBED_MODEL, GEN_MODEL, KitError, guard_scratch  # noqa: E402


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(folder.rglob("*")) if p.is_file()}


def build(folder: Path, scratch: Path, *, gateway=None, parser=None, parse_workers: int = 1) -> dict:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise KitError(f"documents folder not found: {folder}")
    scratch = guard_scratch(scratch, folder)
    if (scratch / "docket.sqlite3").exists():
        raise KitError(f"{scratch} already holds an ingested library; use a new scratch dir (or delete it)")
    forced = {
        "DOCKET_DATA_DIR": str(scratch),
        "DOCKET_KAG_SCOPING": "on",  # builds the document layer + entity graph at ingest
        "DOCKET_PARSE_WORKERS": str(parse_workers),  # 1 keeps RAM modest (each Docling worker needs ~3 GB)
        "DOCKET_GEN_MODEL": GEN_MODEL,
        "DOCKET_EMBED_MODEL": EMBED_MODEL,
    }
    saved = {k: os.environ.get(k) for k in forced}
    os.environ.update(forced)
    from sqlalchemy import text

    from docket.infra.inference.gateway import OllamaGateway
    from docket.interfaces.cli.context import AppContext

    before = snapshot(folder)
    try:
        ctx = AppContext.for_testing(
            data_dir=scratch, gateway=gateway or OllamaGateway(gen_model=GEN_MODEL, embed_model=EMBED_MODEL), parser=parser
        )
    finally:  # settings are already read; do not leak the forced env into the caller's process
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    try:
        source = ctx.source_manager.register_source(folder)
        done = {"n": 0}

        def progress(*a):
            done["n"] += 1

        result = ctx.pipeline.run_ingestion_for_source(source.id, progress)
        failed = sum(1 for r in result.file_results if r.status == "failed")
        total = len(result.file_results)
        with ctx.engine.connect() as c:
            q = lambda s: c.execute(text(s)).scalar() or 0  # noqa: E731
            stats = {
                "files_seen": total, "files_failed": failed,
                "chunks": q("SELECT COUNT(*) FROM chunks"),
                "documents": q("SELECT COUNT(*) FROM documents"),
                "graph_entities": q("SELECT COUNT(*) FROM kag_entities"),
                "graph_document_links": q("SELECT COUNT(*) FROM kag_document_entities"),
            }
    finally:
        ctx.engine.dispose()
    if snapshot(folder) != before:
        raise KitError("the documents folder changed during ingest (it should be read-only) - please report this")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("scratch", type=Path)
    ap.add_argument("--parse-workers", type=int, default=1, help="parallel PDF parsers (default 1; ~3 GB RAM each)")
    a = ap.parse_args(argv)
    try:
        stats = build(a.folder, a.scratch, parse_workers=a.parse_workers)
    except KitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("ingest finished (counts only):")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if not stats["chunks"]:
        print("WARNING: nothing was indexed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
