from docket.cli.interactive.state import SessionState, SourceInfo

HINT = "/ for commands · /exit to quit"


def src(n):
    return tuple(SourceInfo(id=str(i), path=f"/p{i}", status="active") for i in range(n))


def test_zero_sources():
    t = SessionState(model="m").toolbar_text()
    assert t.startswith("no sources — /add <folder>")
    assert HINT in t


def test_singular_and_plural():
    t = SessionState(sources=src(1), turns=1, indexed=True).toolbar_text()
    assert "1 source \u00b7" in t and "1 turn " in t and "1 turns" not in t
    t = SessionState(sources=src(3), turns=2, indexed=True, model="qwen3:14b").toolbar_text()
    assert t == f"3 sources · indexed · mode: auto · 2 turns · qwen3:14b  |  {HINT}"


def test_not_indexed_and_forced_mode():
    t = SessionState(sources=src(2), mode="agent").toolbar_text()
    assert "not indexed yet" in t and "mode: agent" in t


def test_width_drop_order():
    st = SessionState(sources=src(3), indexed=True, turns=2, model="qwen3:14b")
    full = st.toolbar_text()
    no_model = st.toolbar_text(len(full) - 1)
    assert "qwen3" not in no_model and "2 turns" in no_model
    no_turns = st.toolbar_text(len(no_model) - 1)
    assert "turns" not in no_turns and "mode: auto" in no_turns
    no_mode = st.toolbar_text(len(no_turns) - 1)
    assert "mode:" not in no_mode and "indexed" in no_mode
    tiny = st.toolbar_text(5)
    assert tiny.endswith(HINT)
    for t in (full, no_model, no_turns, no_mode, tiny):
        assert HINT in t
