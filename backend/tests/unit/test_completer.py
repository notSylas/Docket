from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from docket.interfaces.cli.interactive.commands import default_registry
from docket.interfaces.cli.interactive.completer import DocketCompleter
from docket.interfaces.cli.interactive.state import SessionState, SourceInfo


def complete(completer, text):
    return list(completer.get_completions(Document(text, len(text)), CompleteEvent()))


def make(sources=()):
    state = SessionState(sources=tuple(sources))
    return DocketCompleter(default_registry(), state)


def test_slash_lists_all_commands_in_order():
    reg = default_registry()
    comps = complete(make(), "/")
    assert [c.text.strip() for c in comps] == [f"/{c.name}" for c in reg.all()]
    for c in comps:
        assert c.start_position == -1
        assert c.display_meta_text
    ingest = next(c for c in comps if c.text.startswith("/ingest"))
    assert ingest.text == "/ingest "
    assert ingest.display_text == "/ingest [<source-id>|all]"
    assert next(c for c in comps if c.text.startswith("/help")).text == "/help"


def test_prefix_narrows():
    comps = complete(make(), "/in")
    assert [c.text for c in comps] == ["/ingest "]
    assert comps[0].start_position == -3


def test_alias_prefix_matches():
    assert [c.text for c in complete(make(), "/ls")] == ["/sources"]
    assert [c.text for c in complete(make(), "/q")] == ["/exit"]


def test_mode_args():
    comps = complete(make(), "/mode ")
    assert [c.text for c in comps] == ["auto", "fast", "agent"]
    comps = complete(make(), "/mode f")
    assert [c.text for c in comps] == ["fast"]
    assert comps[0].start_position == -1


def test_ingest_args_active_only():
    sources = [
        SourceInfo("src_a", "/data/a", "active"),
        SourceInfo("src_b", "/data/b", "revoked"),
        SourceInfo("src_c", "/data/c", "active"),
    ]
    comps = complete(make(sources), "/ingest ")
    assert [c.text for c in comps] == ["all", "src_a", "src_c"]
    assert comps[1].display_meta_text == "/data/a"
    comps = complete(make(sources), "/ingest src_a")
    assert [c.text for c in comps] == ["src_a"]
    assert comps[0].start_position == -5
    assert [c.text for c in complete(make(sources), "/ingest al")] == ["all"]


def test_add_directories_only(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "file.txt").write_text("x")
    comps = complete(make(), f"/add {tmp_path}/")
    assert [c.text for c in comps] == ["docs"]


def test_no_completions_for_plain_text_or_unknown():
    c = make()
    assert complete(c, "what is") == []
    assert complete(c, "") == []
    assert complete(c, " /mode ") == []
    assert complete(c, "/zzz ") == []
    assert complete(c, "/help ") == []


def test_remove_args_active_only_no_all():
    sources = [
        SourceInfo("src_a", "/data/a", "active"),
        SourceInfo("src_b", "/data/b", "revoked"),
    ]
    comps = complete(make(sources), "/remove ")
    assert [c.text for c in comps] == ["src_a"]
    assert comps[0].display_meta_text == "/data/a"


def test_show_args_citation_numbers():
    state = SessionState(citations=((1, "a.docx"), (2, "b.docx")))
    comps = complete(DocketCompleter(default_registry(), state), "/show ")
    assert [c.text for c in comps] == ["1", "2"]
    assert comps[1].display_meta_text == "b.docx"
    assert complete(make(), "/show ") == []
    assert complete(make(), "/retry ") == [] and complete(make(), "/status ") == []
