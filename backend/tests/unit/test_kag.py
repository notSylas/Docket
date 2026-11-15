"""KAG-lite: entity normalisation, graph build, planning (unique / ambiguous / fallback),
clause absence, fusion, migration chain and flag-off neutrality."""
from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from docket.core.config import Settings
from docket.core.db.engine import get_session_factory
from docket.services.documents import builder, kag
from test_document_layer import BLM, BLM2, ZOG, ZOG2, _name, _rec, _alembic_config  # noqa: F401
from test_hybrid_retrieval import _insert_metadata


# --- normalisation -------------------------------------------------------

@pytest.mark.parametrize("raw,core", [
    ("ZOGENIX, INC.", "zogenix"),
    ("Zogenix Inc", "zogenix"),
    ("Reed's, Inc.", "reeds"),
    ("Skype Communications, S.A.", "skype communications"),
    ("ABG-Shaq, LLC", "abg shaq"),
    ("The Bloom Energy Corporation", "bloom energy"),
    ("International Business Machines Corporation", "international business machines"),
])
def test_core_name_strips_suffix_case_punctuation(raw, core):
    assert kag.core_name(raw) == core


def test_variants_include_full_and_core_and_drop_generic():
    assert {"zogenix inc", "zogenix"} <= kag.name_variants("Zogenix, Inc.")
    assert kag.name_variants("Company") == set()
    assert kag.name_variants("The Distributor") == set()
    assert kag.name_variants("Inc.") == set()


def test_clause_type_detection():
    d = lambda q: (kag.detect_clause_type(q) or kag.CLAUSE_TYPES[0]).name  # noqa: E731
    assert d("How often may a party audit the books?") == "audit_rights"
    assert d("What minimum insurance coverage must a party maintain?") == "insurance"
    assert d("Which jurisdiction's law governs the agreement?") == "governing_law"
    assert d("What is the effective date of the agreement?") == "effective_date"
    assert d("How much notice is required to terminate for convenience?") == "termination_for_convenience"
    assert d("Does the agreement restrict a party from assigning it?") == "anti_assignment"
    assert kag.detect_clause_type("Tell me about the weather") is None


def test_classify_chunk():
    got = dict(kag.classify_chunk("Each party shall maintain commercial general liability insurance of $1,000,000."))
    assert "insurance" in got
    assert "audit_rights" not in got


def test_fuse_puts_scoped_first_keeps_global():
    out = kag.fuse(["g1", "g2", "g3", "g4"], ["s1", "g2", "s2"], limit=6)
    ids = [c for c, _ in out]
    assert ids[0] == "g2"            # in both lists
    assert set(ids) == {"g1", "g2", "g3", "g4", "s1", "s2"}
    assert ids.index("s1") < ids.index("g1")


def test_plausible_name_and_filename_entity():
    assert kag.plausible_name("BLOOM ENERGY CORPORATION")
    assert not kag.plausible_name("other things, agreed to")
    assert not kag.plausible_name("Reed's Intellectual Property. Company")
    assert not kag.plausible_name("NETGEAR Agreement Number: N115 Effective Date: March 1")
    assert kag.filename_entity("ReedsInc_20191113_10-Q_EX-10.4_Development Agreement.pdf") == "Reeds Inc"
    assert kag.filename_entity("2020_contract.pdf") is None
    assert kag.filename_entity("Agreement.pdf") is None


def test_all_caps_corporation_found_in_preamble():
    ents = dict(kag.preamble_entities([], "MASTER AGREEMENT by and between DIAMOND STATE GENERATION PARTNERS, LLC and BLOOM ENERGY CORPORATION dated as of April 13, 2012"))
    assert any(kag.core_name(d) == "bloom energy" for d in ents)


# --- migration chain -----------------------------------------------------

def test_single_alembic_head():
    cfg = Config()
    cfg.set_main_option("script_location", str(__import__("pathlib").Path(kag.__file__).resolve().parents[2] / "core" / "db" / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["f6c9e3a18e36"]
    assert script.get_revision("f6c9e3a18e36").down_revision == "f6c9e3a18d25"


def test_migration_creates_and_drops_graph_tables(tmp_path):
    from alembic import command
    from docket.core.db.engine import get_engine

    p = tmp_path / "d.sqlite3"
    cfg = _alembic_config(p)
    command.upgrade(cfg, "head")
    eng = get_engine(p)
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
    assert {"kag_entities", "kag_entity_variants", "kag_document_entities", "kag_document_clauses"} <= names
    eng.dispose()
    command.downgrade(cfg, "f6c9e3a18d25")
    eng = get_engine(p)
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
    assert not {n for n in names if n.startswith("kag_")}
    assert "documents" in names
    eng.dispose()


# --- graph build and planning -------------------------------------------

ZOG3 = "Each party shall maintain insurance coverage of not less than $5,000,000 per occurrence."


@pytest.fixture
def lib(migrated_sqlite_engine):
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_z0", "ev_z", ZOG, 0), _rec("c_z1", "ev_z", ZOG2, 1), _rec("c_z2", "ev_z", ZOG3, 2)])
    _insert_metadata(e, [_rec("c_b0", "ev_b", BLM, 0), _rec("c_b1", "ev_b", BLM2, 1)])
    _name(e, "ev_z", "/lib/Zogenix_Distributor Agreement.pdf", 100.0)
    _name(e, "ev_b", "/lib/Bloom_Maintenance Agreement.pdf", 100.0)
    sf = get_session_factory(e)
    builder.build_all(sf, kag=True)
    return e, sf


def test_graph_rows_point_to_source_chunks(lib):
    e, _ = lib
    with e.connect() as c:
        ents = {r[0] for r in c.execute(text("SELECT norm FROM kag_entities"))}
        edges = c.execute(text("SELECT chunk_id FROM kag_document_entities")).scalars().all()
        links = c.execute(text("SELECT clause_type, chunk_id FROM kag_document_clauses")).all()
    assert "zogenix" in ents and "bloom energy" in ents
    assert edges and all(x for x in edges)
    assert ("insurance", "c_z2") in links
    assert ("governing_law", "c_z1") in links


def test_unique_match_and_clause_chunks(lib):
    e, _ = lib
    p = kag.plan(e, "What minimum insurance must a party maintain under the Distributor Agreement with Zogenix, Inc.?")
    assert p.active and p.mode == "unique" and p.version_ids == ["ev_z"]
    assert "c_z2" in p.clause_chunk_ids and not p.clause_absent


def test_clause_absent_is_flagged_for_unique_match(lib):
    e, _ = lib
    p = kag.plan(e, "How often may a party audit the books under the agreement with Bloom Energy Corporation?")
    assert p.active and p.mode == "unique" and p.clause.name == "audit_rights"
    assert p.clause_absent is True
    assert "contains no audit rights" in kag.absent_note(p)


def test_governing_law_never_declared_absent(lib):
    e, _ = lib
    p = kag.plan(e, "Which law governs the Bloom Energy agreement?")
    assert p.active and not p.clause_absent


def test_no_entity_means_inactive(lib):
    e, _ = lib
    p = kag.plan(e, "Which agreements restrict assignment?")
    assert not p.active and p.mode == "none"


def test_unknown_entity_falls_back(lib):
    e, _ = lib
    p = kag.plan(e, "What is the governing law of the agreement with Acme Rocketry Inc?")
    assert not p.active


def test_two_documents_are_not_exclusive(lib):
    e, sf = lib
    # second, different contract that also names Zogenix
    _insert_metadata(e, [_rec("c_y0", "ev_y", ZOG.replace("Sumitomo Dainippon Pharma", "Other Pharma Ltd") + " Different text here.", 0)])
    _name(e, "ev_y", "/lib/Zogenix_Second.pdf", 100.0)
    builder.build_all(get_session_factory(e), kag=True)
    p = kag.plan(e, "What is the effective date of the agreement with Zogenix?")
    assert p.active and p.mode == "multi" and len(p.version_ids) == 2
    assert p.clause_absent is False  # never claim absence across several documents


def test_scope_excludes_documents(lib):
    e, _ = lib
    p = kag.plan(e, "Which law governs the Zogenix agreement?", scope_version_ids=["ev_b"])
    assert not p.active


def test_plan_never_raises(migrated_sqlite_engine):
    # no graph rows at all (flag was off at ingest)
    p = kag.plan(migrated_sqlite_engine, "Which law governs the Zogenix agreement?")
    assert not p.active


# --- flag-off neutrality -------------------------------------------------

def test_flag_default_off_and_build_unchanged(migrated_sqlite_engine):
    assert Settings().kag_scoping == "off"
    assert Settings().documents_build_enabled is False
    e = migrated_sqlite_engine
    _insert_metadata(e, [_rec("c_z0", "ev_z", ZOG, 0), _rec("c_z1", "ev_z", ZOG2, 1)])
    _name(e, "ev_z", "/lib/Zogenix_Distributor Agreement.pdf", 100.0)
    builder.build_all(get_session_factory(e))  # kag off
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM kag_entities")).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM kag_document_clauses")).scalar_one() == 0


def test_kag_flag_implies_document_build():
    assert Settings(kag_scoping="on").documents_build_enabled is True


def test_deleting_document_cascades_graph(lib):
    e, sf = lib
    builder.delete_for_version(sf, "ev_z")
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM kag_document_entities")).scalar_one() > 0  # Bloom's edges stay
        assert c.execute(text("SELECT COUNT(*) FROM kag_document_entities WHERE document_id NOT IN (SELECT id FROM documents)")).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM kag_document_clauses WHERE chunk_id LIKE 'c_z%'")).scalar_one() == 0
