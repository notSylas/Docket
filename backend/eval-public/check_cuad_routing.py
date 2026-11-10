"""CPU-only check of document routing + field lookup on gold-cuad.yaml.

Builds a scratch library from the CUAD full-text versions of the 12 gold contracts
(same chunker as ingestion, no model), then for each gold question reports whether
routing named the gold contract, and whether field lookup answered and matched the
gold `must_contain`. Never touches the user's data dir.

    backend/.venv/bin/python backend/eval-public/check_cuad_routing.py --cuad <dir with CUAD_v1/> --scratch <empty dir>
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuad", required=True)
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--distractors", type=int, default=0, help="extra CUAD contracts added as look-alike distractors")
    ap.add_argument("--gold", default=str(Path(__file__).with_name("gold-cuad.yaml")))
    a = ap.parse_args()
    scratch = Path(a.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["DOCKET_DATA_DIR"] = str(scratch)

    from alembic import command
    from alembic.config import Config

    from docket.core.config import Settings, settings
    from docket.core.db.engine import get_engine, get_session_factory
    from docket.core.db.models import (
        AuthorizedSource, Chunk, ChunkRecipe, EvidenceUnit, EvidenceVersion, Source, VersionStatus, Workspace,
    )
    from docket.infra.parsing.chunker import chunk_document
    from docket.infra.parsing.recipes import docling_recipe
    from docket.infra.retrieval.resolver import EvidenceResolver
    from docket.services.documents import builder
    from docket.services.documents.layer import DocumentLayer

    root = Path(a.cuad) / "CUAD_v1" / "full_contract_txt"
    gold = yaml.safe_load(open(a.gold))
    docs = sorted({d for q in gold["questions"] for d in q.get("source_documents", [])})
    extra = [f"contracts/{p.stem}.pdf" for p in sorted(root.glob("*.txt")) if f"contracts/{p.stem}.pdf" not in set(docs)][: a.distractors]
    docs = docs + extra
    sqlite = scratch / "docket.sqlite3"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "src/docket/core/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite}")
    command.upgrade(cfg, "head")
    engine = get_engine(sqlite)
    sf = get_session_factory(engine)
    recipe = docling_recipe(settings, "docling", "1")
    vid_of: dict[str, str] = {}
    with sf() as s:
        s.add(Workspace(id="ws", name="ws"))
        s.add(AuthorizedSource(id="auth", workspace_id="ws", scope_path="/x"))
        s.add(Source(id="src", workspace_id="ws", authorized_source_id="auth", source_type="local_folder", path="/x"))
        s.add(ChunkRecipe(id="rcp", chunk_size=200, overlap=40, splitter="t", parser_name="t", parser_version="1"))
        for i, rel in enumerate(docs):
            stem = Path(rel).stem
            txt = (root / f"{stem}.txt")
            if not txt.exists():
                print("missing txt for", stem)
                continue
            vid = f"ev{i}"
            vid_of[rel] = vid
            s.add(EvidenceVersion(id=vid, source_id="src", file_path=f"/x/{Path(rel).name}", content_hash=f"h{i}", byte_size=1,
                                  parser_name="t", parser_version="1", status=VersionStatus.READY, source_mtime=float(i)))
            s.flush()
            _, drafts = chunk_document(txt.read_text(encoding="utf-8", errors="replace"), recipe)
            s.add(EvidenceUnit(id=f"eu{i}", evidence_version_id=vid, unit_index=0, content_hash=f"uh{i}"))
            s.flush()
            for j, d in enumerate(drafts):
                s.add(Chunk(id=f"c{i}_{j}", source_id="src", evidence_version_id=vid, evidence_unit_id=f"eu{i}",
                            chunk_recipe_id="rcp", ordinal=j, heading=getattr(d, "heading", None), text=d.text, content_hash=f"x{i}_{j}"))
        s.commit()
    builder.build_all(sf)
    layer = DocumentLayer(engine, sf, Settings(doc_routing="on", field_lookup="on"))
    resolver = EvidenceResolver(sf)
    routed = unique = right = looked = looked_ok = wrong_answers = answerable = 0
    for q in gold["questions"]:
        answerable += bool(q.get("answerable", True))
        want = {vid_of.get(d) for d in q.get("source_documents", [])}
        dec = layer.route(q["question"], None)
        hit = bool(dec and dec.routed and want & set(dec.version_ids))
        routed += bool(dec and dec.routed)
        unique += bool(dec and dec.routed and dec.top_unique)
        right += hit
        ans = layer.lookup_answer(q["question"], dec, resolver)
        tag = "route=%s" % ("hit" if hit else ("MISS" if dec and dec.routed else "fallback:" + (dec.reason if dec else "off")))
        if ans:
            looked += 1
            ok = q.get("answerable", True) and all(re.search(m[3:], ans[0].answer, re.I) if m.startswith("re:") else m.lower() in ans[0].answer.lower()
                     for m in q.get("must_contain", []))
            looked_ok += ok
            wrong_answers += not ok
            tag += f" lookup={'OK' if ok else 'WRONG'}({ans[0].field})"
        print(f"{q['id'][:34]:34} {tag}")
    print(f"answerable={answerable} routed={routed} routed_unique={unique} routed_contains_gold_doc={right} "
          f"lookup_answered={looked} lookup_correct={looked_ok} lookup_wrong={wrong_answers}")


if __name__ == "__main__":
    sys.exit(main())
