"""Lane A KAG evaluation: schema/loader, silver derivation and deterministic metrics.

Metrics are checked on a hand-built fixture graph (two contracts) with hand-written gold, so every
expected number follows from the fixture; nothing here is a measurement of the real corpus.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml
from test_kag_lifecycle import lib  # noqa: F401  (fixture: two contracts, graph built)
from typer.testing import CliRunner

from docket.eval.kag import metrics as M
from docket.eval.kag.graph import RealDataDirError, connect_ro, load_snapshot
from docket.eval.kag.retrieval import LexicalIndex, retrieve
from docket.eval.kag.review_sheet import export_review_sheets
from docket.eval.kag.run import format_kag_report, report_to_json, run_kag_eval
from docket.eval.kag.schema import (
    KagGold,
    KagGoldError,
    load_kag_gold,
    provenance_counts,
    select_items,
    validate_against_library,
)
from docket.eval.kag.scratch import ScratchError, build_scratch_library
from docket.eval.kag.silver import build_silver, write_gold_yaml
from docket.interfaces.cli.main import app

runner = CliRunner()
ZOG_F = "contracts/Zogenix_Distributor Agreement.pdf"
BLM_F = "contracts/Bloom_Maintenance Agreement.pdf"
EVAL_PUBLIC = Path(__file__).resolve().parents[4] / "evals" / "suites"


def db_path(engine) -> str:
    return str(engine.url.database)


@pytest.fixture
def snap_conn(lib):  # noqa: F811
    e, _ = lib
    conn = sqlite3.connect(db_path(e))
    yield load_snapshot(conn), conn
    conn.close()


def gold(**sections) -> KagGold:
    return KagGold.model_validate({"version": 1, **sections})


# -- schema ----------------------------------------------------------------------------------


def test_loader_rejects_bad_gold(tmp_path):
    cases = {
        "bad path": {"clauses": [{"id": "c", "file": "/abs/x.pdf", "clause_type": "insurance", "present": True}]},
        "dotdot": {"clauses": [{"id": "c", "file": "../x.pdf", "clause_type": "insurance", "present": True}]},
        "clause type": {"clauses": [{"id": "c", "file": "a.pdf", "clause_type": "nope", "present": True}]},
        "human no reviewer": {"roles": [{"id": "r", "file": "a.pdf", "entity": "X", "role": "party",
                                         "label_source": "human_reviewed", "human_reviewed": True}]},
        "flag without source": {"roles": [{"id": "r", "file": "a.pdf", "entity": "X", "role": "party", "human_reviewed": True}]},
        "dup ids": {"roles": [{"id": "r", "file": "a.pdf", "entity": "X", "role": "party"},
                              {"id": "r", "file": "a.pdf", "entity": "Y", "role": "party"}]},
        "present and absent": {"clauses": [{"id": "a", "file": "a.pdf", "clause_type": "insurance", "present": True},
                                           {"id": "b", "file": "a.pdf", "clause_type": "insurance", "present": False}]},
        "absent with spans": {"clauses": [{"id": "a", "file": "a.pdf", "clause_type": "insurance", "present": False,
                                           "gold_spans": ["x" * 25]}]},
        "unknown field": {"roles": [{"id": "r", "file": "a.pdf", "entity": "X", "role": "party", "oops": 1}]},
        "bad role": {"roles": [{"id": "r", "file": "a.pdf", "entity": "X", "role": "boss"}]},
    }
    for name, body in cases.items():
        p = tmp_path / "g.yaml"
        p.write_text(yaml.safe_dump({"version": 1, **body}))
        with pytest.raises(KagGoldError):
            load_kag_gold(p)
        assert name
    with pytest.raises(KagGoldError, match="cannot read"):
        load_kag_gold(tmp_path / "missing.yaml")


def test_provenance_selection_and_library_check():
    g = gold(roles=[
        {"id": "a", "file": "a.pdf", "entity": "X", "role": "party"},
        {"id": "b", "file": "a.pdf", "entity": "Y", "role": "party", "label_source": "human_reviewed",
         "human_reviewed": True, "reviewed_by": "tester", "review_status": "confirmed"},
        {"id": "c", "file": "a.pdf", "entity": "Z", "role": "party", "review_status": "rejected"},
    ])
    assert [i.id for i in select_items(g.roles)] == ["a", "b"]  # rejected never counts
    assert [i.id for i in select_items(g.roles, human_only=True)] == ["b"]
    assert provenance_counts(g.roles) == {"human_reviewed": 1, "silver_unreviewed": 1}
    assert validate_against_library(g, {"x/b.pdf"}) == ["gold references a document that is not in the library: a.pdf"]
    assert validate_against_library(g, {"x/a.pdf"}) == []


# -- metrics on the fixture graph ------------------------------------------------------------


def test_entity_extraction_exact(snap_conn):
    snap, _ = snap_conn
    docs = gold(entities=[
        {"id": "z", "file": ZOG_F, "entities": [{"name": "Zogenix, Inc."}, {"name": "Sumitomo Dainippon Pharma"},
                                                {"name": "Acme Corp"}]},
        {"id": "b", "file": BLM_F, "complete": True, "entities": [{"name": "Bloom Energy Corporation"},
                                                                  {"name": "Pacific Utilities LLC"}]},
    ]).entities
    r = M.entity_extraction(docs, snap, n_boot=200)
    assert (r.recall.n, r.recall.value) == (5, pytest.approx(4 / 5))  # Acme missing
    assert (r.precision.n, r.precision.value) == (3, pytest.approx(2 / 3))  # filename entity "Bloom" is extra
    assert r.recall.ci[0] <= r.recall.value <= r.recall.ci[1]
    assert r.per_doc[ZOG_F] == (3, 2, 2)
    # a document not in the library is reported, not silently scored
    assert M.entity_extraction(gold(entities=[{"id": "q", "file": "contracts/Nope.pdf", "entities": [{"name": "A B"}]}]).entities,
                               snap, n_boot=50).missing_docs == ["contracts/Nope.pdf"]


def test_resolution_pairs_exact(snap_conn):
    snap, _ = snap_conn
    pairs = gold(resolution_pairs=[
        {"id": "1", "a": "Zogenix, Inc.", "b": "ZOGENIX INC", "same": True},  # tp
        {"id": "2", "a": "Bloom Energy Corporation", "b": "Bloom", "same": True},  # fn: filename entity is a separate node
        {"id": "3", "a": "Zogenix", "b": "Pacific Utilities LLC", "same": False},  # tn
        {"id": "4", "a": "Sumitomo Dainippon Pharma", "b": "Zogenix", "same": False},  # tn
        {"id": "5", "a": "Pacific Utilities LLC", "b": "Pacific Utilities Holdings", "same": False},  # fp: false merge
        {"id": "6", "a": "Zogenix", "b": "Nobody Corp", "same": False},  # unresolved name
    ]).resolution_pairs
    r = M.entity_resolution(pairs, snap, n_boot=200)
    assert r.confusion == {"tp": 1, "fp": 1, "fn": 1, "tn": 3, "pairs_with_unresolved_name": 1}
    assert r.precision.value == pytest.approx(0.5) and r.recall.value == pytest.approx(0.5)
    assert (r.false_merge_rate.n, r.false_merge_rate.value) == (3, pytest.approx(1 / 3))


def test_role_and_relation_metrics(snap_conn):
    snap, conn = snap_conn
    roles = gold(roles=[
        {"id": "1", "file": ZOG_F, "entity": "Zogenix, Inc.", "role": "party"},
        {"id": "2", "file": BLM_F, "entity": "Bloom Energy Corporation", "role": "mention"},  # graph says party
        {"id": "3", "file": BLM_F, "entity": "Nobody Corp", "role": "party"},  # no edge
    ]).roles
    present, correct = M.role_correctness(roles, snap, n_boot=100)
    assert (present.n, present.value) == (3, pytest.approx(2 / 3))
    assert correct.value == pytest.approx(1 / 3)
    rel = gold(relations=[{"id": "r", "from_file": ZOG_F, "to_file": BLM_F, "relation": "supersedes"},
                          {"id": "s", "from_file": BLM_F, "to_file": ZOG_F, "relation": "none"}]).relations
    out = M.relation_prf(rel, M.graph_relation_predictor(conn, snap), n_boot=100)
    assert out["supersedes_recall"].value == 0.0 and out["supersedes_recall"].n == 1
    assert out["supersedes_precision"].n == 0 and out["amends_recall"].n == 0  # nothing predicted / labelled


def test_clause_absence_exact(snap_conn):
    snap, _ = snap_conn
    items = gold(clauses=[
        {"id": "1", "file": ZOG_F, "clause_type": "insurance", "present": False},  # graph: no edge -> absent, correct
        {"id": "2", "file": BLM_F, "clause_type": "audit_rights", "present": True},  # graph: no edge -> wrongly absent
        {"id": "3", "file": ZOG_F, "clause_type": "governing_law", "present": True},  # has edge
        {"id": "4", "file": ZOG_F, "clause_type": "parties", "present": False},  # cannot-be-absent type: skipped
    ]).clauses
    p, r = M.clause_absence(items, snap, n_boot=100)
    assert (p.n, p.value) == (2, pytest.approx(0.5)) and (r.n, r.value) == (1, 1.0)


def test_clause_recall_paired_exact():
    items = gold(clauses=[
        {"id": f"c{i}", "file": ZOG_F, "clause_type": "governing_law", "present": True, "question": "q",
         "gold_spans": ["laws of the state of new york"]} for i in range(3)
    ] + [{"id": "abs", "file": ZOG_F, "clause_type": "insurance", "present": False, "question": "q"}]).clauses
    needle = "This Agreement is governed by the laws of the State of New York."
    off = {"c0": ["x", "y", needle], "c1": ["x", "y", "z"], "c2": [needle, "x", "y"]}
    on = {"c0": [needle, "x", "y"], "c1": ["x", "y", "z"], "c2": [needle, "x", "y"], "abs": []}
    res = M.clause_recall_at_k(items, off, on, ks=(1, 3), n_boot=200)
    assert res.off[1].value == pytest.approx(1 / 3) and res.on[1].value == pytest.approx(2 / 3)
    assert res.off[3].value == pytest.approx(2 / 3) and res.on[3].value == pytest.approx(2 / 3)
    assert res.off[1].n == 3  # the absent item never enters recall
    assert res.paired[1]["flips"] == {"both": 1, "a_only": 0, "b_only": 1, "neither": 1}
    assert res.paired[1]["label"] == "statistically inconclusive"  # one flip is never conclusive


def test_provenance_clean_and_corrupted(lib):  # noqa: F811
    e, _ = lib
    path = db_path(e)
    conn = sqlite3.connect(path)
    pv = M.provenance_coverage(conn, load_snapshot(conn), n_boot=100)
    assert (pv.entity_edges_resolve.n, pv.entity_edges_resolve.value) == (5, 1.0)
    assert (pv.entity_edges_contain.n, pv.entity_edges_contain.value) == (4, 1.0)  # filename edge excluded
    assert pv.entity_edges_filename_only == 1
    assert (pv.clause_edges_resolve.n, pv.clause_edges_contain.value) == (7, 1.0)
    conn.close()
    with e.begin() as c:  # one dangling chunk id, one chunk that belongs to a different document
        c.exec_driver_sql("UPDATE kag_document_entities SET chunk_id='c_missing' WHERE document_id=1 AND entity_id=1")
        c.exec_driver_sql("UPDATE kag_document_entities SET chunk_id='c_b1' WHERE document_id=1 AND entity_id=2")
    conn = sqlite3.connect(path)
    pv = M.provenance_coverage(conn, load_snapshot(conn), n_boot=100)
    assert pv.entity_edges_resolve.value == pytest.approx(3 / 5)
    assert pv.entity_edges_contain.value == pytest.approx(2 / 4)
    conn.close()


def test_lifecycle_consistency(lib):  # noqa: F811
    e, _ = lib
    path = db_path(e)
    conn = sqlite3.connect(path)
    lc = M.lifecycle_consistency(conn, load_snapshot(conn), n_boot=100)
    assert (lc.consistent_edges.n, lc.consistent_edges.value, lc.stale_edges, lc.orphan_entities) == (12, 1.0, 0, 0)
    conn.close()
    with e.begin() as c:
        c.exec_driver_sql("UPDATE sources SET status='REVOKED' WHERE id='src_z'")
        c.exec_driver_sql("DELETE FROM kag_document_entities WHERE entity_id=5")
    conn = sqlite3.connect(path)
    lc = M.lifecycle_consistency(conn, load_snapshot(conn), n_boot=100)
    assert lc.stale_edges == 7 and lc.consistent_edges.value == pytest.approx(4 / 11)  # doc 1: 2 entity + 5 clause edges
    assert lc.orphan_entities == 1  # the purge finding: an entity with no document edge
    conn.close()


# -- retrieval, runner, scratch build, review sheets -----------------------------------------


def test_lexical_scoping_ranks_clause_chunk_first(lib):  # noqa: F811
    e, _ = lib
    from docket.eval.kag.graph import engine_ro

    conn = connect_ro(db_path(e))
    index = LexicalIndex.from_db(conn)
    eng = engine_ro(db_path(e))
    q = "Which law governs the Distributor Agreement with Zogenix, Inc.?"
    on = retrieve(index, eng, q, scoping=True)
    off = retrieve(index, eng, q, scoping=False)
    assert on[0] == "c_z1" and set(on) >= set(off[:1])
    eng.dispose()
    conn.close()


def test_run_kag_eval_end_to_end_and_redaction(lib):  # noqa: F811
    e, _ = lib
    g = gold(
        entities=[{"id": "z", "file": ZOG_F, "entities": [{"name": "Zogenix, Inc."}]}],
        clauses=[{"id": "c", "file": ZOG_F, "clause_type": "governing_law", "present": True,
                  "question": "Which law governs the Distributor Agreement with Zogenix, Inc.?",
                  "gold_spans": ["laws of the State of New York"]}],
    )
    rep = run_kag_eval(g, db_path(e), ks=(1, 3), n_boot=100)
    assert rep.all_silver and rep.metrics["entity_recall"].value == 1.0
    assert rep.metrics["clause_recall@1_on"].value == 1.0 and rep.metrics["clause_recall@1_on"].n == 1
    text = format_kag_report(rep)
    assert "SILVER, UNREVIEWED" in text and "n=1" in text
    red = report_to_json(rep, redact=True)
    assert "Zogenix" not in red and db_path(e) not in red
    assert run_kag_eval(g, db_path(e), ks=(1,), human_only=True, n_boot=50).metrics["entity_recall"].n == 0


def test_real_data_dir_is_refused(tmp_path):
    with pytest.raises(RealDataDirError):
        connect_ro(Path.home() / ".local" / "share" / "docket" / "docket.sqlite3")
    with pytest.raises(RealDataDirError):
        build_scratch_library(tmp_path / "x.sqlite3", [], Path.home() / ".local/share/docket/scratch")


def test_scratch_library_build_from_stored_chunks(lib, tmp_path):  # noqa: F811
    e, _ = lib
    info = build_scratch_library(Path(db_path(e)), [ZOG_F], tmp_path / "scratch")
    assert info["documents"] == 1 and info["built"] == 1
    assert stat.S_IMODE(os.stat(tmp_path / "scratch").st_mode) == 0o700
    conn = connect_ro(tmp_path / "scratch" / "docket.sqlite3")
    snap = load_snapshot(conn)
    assert set(snap.docs_by_file) == {"Zogenix_Distributor Agreement.pdf"} and len(snap.entities) >= 2
    conn.close()
    with pytest.raises(ScratchError, match="already exists"):
        build_scratch_library(Path(db_path(e)), [ZOG_F], tmp_path / "scratch")
    with pytest.raises(ScratchError, match="no servable version"):
        build_scratch_library(Path(db_path(e)), ["contracts/Missing.pdf"], tmp_path / "scratch2")


def test_review_sheets_private_and_unlabelled(snap_conn, tmp_path):
    snap, conn = snap_conn
    out = tmp_path / "rev"
    s = export_review_sheets(conn, snap, out)
    assert s["entity_rows"] == 5 + 2 and s["resolution_rows"] >= 1
    for p in s["paths"].values():
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o700
    import csv

    for name, cols in (("kag_review_entities.csv", ("entity_correct", "role_correct", "missing_entities")),
                       ("kag_review_resolution.csv", ("same",))):
        rows = list(csv.DictReader((out / name).open(newline="")))
        assert rows and all(r[c] == "" for r in rows for c in cols)


# -- silver derivation -----------------------------------------------------------------------


def test_silver_is_mechanical_and_never_human():
    g = build_silver([EVAL_PUBLIC / "gold-cuad.yaml", EVAL_PUBLIC / "gold-cuad-scale.yaml"])
    items = g.all_items()
    assert items and all(i.label_source == "silver_unreviewed" and not i.human_reviewed and i.reviewed_by is None
                         and i.derivation for i in items)
    assert all(not e.complete for e in g.entities)  # a silver list never claims to be complete
    assert {(c.file, c.clause_type): c.present for c in g.clauses}[
        ("contracts/ReedsInc_20191113_10-Q_EX-10.4_11888303_EX-10.4_Development Agreement.pdf", "insurance")] is False
    assert any(p.same for p in g.resolution_pairs) and any(not p.same for p in g.resolution_pairs)


def test_committed_silver_gold_is_fresh_and_valid(tmp_path):
    committed = load_kag_gold(EVAL_PUBLIC / "kag" / "kag-gold-cuad12.silver.yaml")
    fresh = build_silver([EVAL_PUBLIC / "gold-cuad.yaml", EVAL_PUBLIC / "gold-cuad-scale.yaml"], corpus="CUAD v1 12-contract slice")
    assert committed.model_dump() == fresh.model_dump()
    assert all(i.label_source == "silver_unreviewed" for i in committed.all_items())
    write_gold_yaml(fresh, tmp_path / "x.yaml")
    assert load_kag_gold(tmp_path / "x.yaml").model_dump() == fresh.model_dump()


# -- CLI ---------------------------------------------------------------------------------------


def test_cli_kag_run_and_silver(lib, tmp_path):  # noqa: F811
    e, _ = lib
    g = tmp_path / "g.yaml"
    g.write_text(yaml.safe_dump({"version": 1, "entities": [{"id": "z", "file": ZOG_F, "entities": [{"name": "Zogenix, Inc."}]}]}))
    out = tmp_path / "rep.txt"
    r = runner.invoke(app, ["eval", "kag", "run", "--db", db_path(e), "--gold", str(g), "--ks", "3", "--n-boot", "100",
                            "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "Entity extraction recall" in r.output and "100.0%" in r.output and stat.S_IMODE(os.stat(out).st_mode) == 0o600
    bad = runner.invoke(app, ["eval", "kag", "run", "--db", str(Path.home() / ".local/share/docket/docket.sqlite3"), "--gold", str(g)])
    assert bad.exit_code == 1 and "refusing" in bad.output
    s = runner.invoke(app, ["eval", "kag", "silver", "--out", str(tmp_path / "s.yaml")])
    assert s.exit_code == 0 and "silver_unreviewed" in s.output
    assert "kag" in runner.invoke(app, ["eval", "--help"]).output
