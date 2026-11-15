"""Query-only CUAD run on a CLEAN library: each contract present once.

`cuad_scale_curve.py` registers a new source per stage, so the 12 gold contracts end up ingested once per
stage (3 copies at stage 200) and the same distractors twice. This script copies a finished scale data dir,
marks every source outside --keep as REVOKED (hybrid search, the document layer and the entity graph all skip
non-ACTIVE sources), and runs the gold questions without ingesting anything.

    python cuad_query_clean.py SRC_DATA_DIR WORK_DIR OUT.jsonl --keep src_x src_y [--repeats 1]
"""
import argparse, shutil, sqlite3
from pathlib import Path


def main():
    from docket.core.config import Settings
    from docket.eval.runner import EvalRunner, OllamaMetaGateway, select_questions
    from docket.eval.schema import load_gold_set, fingerprint

    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path); ap.add_argument("work", type=Path); ap.add_argument("out", type=Path)
    ap.add_argument("--keep", nargs="+", required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--gold", type=Path, default=Path(__file__).resolve().parent.parent / "gold-cuad-scale.yaml")
    ap.add_argument("--corpus", type=Path, required=True, help="any folder with one sub-folder (never ingested)")
    a = ap.parse_args()
    for p in (a.src, a.work):
        assert "/.local/share/docket" not in str(p.resolve())
    if not a.work.exists():
        shutil.copytree(a.src, a.work)
    db = sqlite3.connect(a.work / "docket.sqlite3")
    ids = [r[0] for r in db.execute("SELECT id FROM sources")]
    assert all(k in ids for k in a.keep), (a.keep, ids)
    db.execute("UPDATE sources SET status='ACTIVE' WHERE id IN (%s)" % ",".join("?" * len(a.keep)), a.keep)
    db.execute("UPDATE sources SET status='REVOKED' WHERE id NOT IN (%s)" % ",".join("?" * len(a.keep)), a.keep)
    db.commit(); db.close()
    gold = load_gold_set(a.gold)
    qs = select_questions(gold, split=None)
    with EvalRunner(a.corpus, gateway=OllamaMetaGateway(gen_model=Settings().gen_model), data_dir=a.work) as runner:
        runner._sources = {}  # query-only: nothing is ingested
        runner.ingest()
        runner.run(qs, repeats=a.repeats, out_path=a.out, gold_digest=fingerprint(gold),
                   progress=lambda d, t, r: print(f"{d}/{t} {r.question_id}#{r.repeat} {r.latency_s:.1f}s"
                                                  + (f" ERROR {r.error}" if r.error else ""), flush=True))


if __name__ == "__main__":
    main()
