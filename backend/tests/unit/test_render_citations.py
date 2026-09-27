from docket.interfaces.cli.interactive.render import number_citations
from docket.services.query.service import Citation


def C(label, cid="x"):
    return Citation(citation_label=label, chunk_id=cid, source_display_name="s")


def test_order_by_first_appearance_and_repeats():
    a, b = C("[a #x]", "a"), C("[b #y]", "b")
    text, out = number_citations("one [b #y] two [a #x] three [b #y]", [a, b])
    assert text == "one [1] two [2] three [1]"
    assert [c.chunk_id for c in out] == ["b", "a"]


def test_adjacent_tags():
    text, out = number_citations("x [a #x] [b #y]", [C("[a #x]"), C("[b #y]")])
    assert text == "x [1] [2]" and len(out) == 2


def test_unknown_tag_untouched():
    text, out = number_citations("x [a #x] [z #zz]", [C("[a #x]")])
    assert text == "x [1] [z #zz]" and len(out) == 1


def test_absent_label_appended():
    a, b = C("[a #x]", "a"), C("[b #y]", "b")
    text, out = number_citations("only [b #y]", [a, b])
    assert text == "only [1]"
    assert [c.chunk_id for c in out] == ["b", "a"]


def test_no_citations_unchanged():
    assert number_citations("plain [a #x]", []) == ("plain [a #x]", [])


def test_similar_labels_no_cross_corruption():
    a, b = C("[a.docx #chk_1234abcd]", "a"), C("[a.docx #chk_1234abce]", "b")
    text, out = number_citations("[a.docx #chk_1234abce] then [a.docx #chk_1234abcd]", [a, b])
    assert text == "[1] then [2]"
    assert [c.chunk_id for c in out] == ["b", "a"]


def test_prefix_label_longest_first():
    short, long = C("[a #x]", "s"), C("[a #x] extra", "l")
    text, _ = number_citations("v [a #x] extra", [short, long])
    assert text == "v [1]"


def test_duplicate_citation_entries_deduped():
    text, out = number_citations("[a #x]", [C("[a #x]"), C("[a #x]")])
    assert text == "[1]" and len(out) == 1
