import codecs
import json

import pytest

from docket.infra.parsing.filecheck import InvalidFileError, check_file_signature
from docket.infra.parsing.text_family import TextFamilyParser, chunk_text, decode_text


@pytest.mark.parametrize("data,encoding,lossy,expected", [
    (b"hello", "utf-8", False, "hello"),
    ("नमस्ते বাংলা".encode(), "utf-8", False, "नमस्ते বাংলা"),
    (codecs.BOM_UTF8 + b"hello", "utf-8-sig", False, "hello"),
    (codecs.BOM_UTF16_LE + "hello".encode("utf-16-le"), "utf-16", False, "hello"),
    (codecs.BOM_UTF16_BE + "hello".encode("utf-16-be"), "utf-16", False, "hello"),
    ("hello".encode("utf-16-le"), "utf-16-le", False, "hello"),
    ("hello".encode("utf-16-be"), "utf-16-be", False, "hello"),
    (b"caf\xe9", "cp1252-fallback", True, "café"),
    (b"bad\x81", "cp1252-fallback", True, "bad\ufffd"),
    (codecs.BOM_UTF16_LE + b"x", "utf-16", True, "\ufffd"),
    ("hello".encode("utf-32"), "utf-32", False, "hello"),
])
def test_encodings(data, encoding, lossy, expected):
    decoded = decode_text(data)
    assert (decoded.encoding, decoded.lossy, decoded.text) == (encoding, lossy, expected)


@pytest.mark.parametrize("text,value,column", [
    ('name,note\nAda,"first\nsecond"\n', "first\nsecond", "note"),
    ('code,amount\n0012,"1,23,456.78"\n', "1,23,456.78", "amount"),
    ('code,amount\n0012,20\n', "0012", "code"),
    ('name;note\nAda;hello\n', "hello", "note"),
    ('name\tnote\nAda\thello\n', "hello", "note"),
    ('name|note\nAda|hello\n', "hello", "note"),
    ('name,note\nAda,"say ""hello"""\n', 'say "hello"', "note"),
    ('name,note\nAda,=WEBSERVICE("http://invalid")\n', '=WEBSERVICE("http://invalid")', "note"),
    ('name,note\nAda,+SUM(A1:A2)\n', "+SUM(A1:A2)", "note"),
    ('name,note\nAda,@cmd\n', "@cmd", "note"),
    ('name,note\r\nAda,"x\r\ny"\r\n', "x\r\ny", "note"),
    ('name,note\nAda,-1+2\n', "-1+2", "note"),
])
def test_csv_cells(tmp_path, text, value, column):
    path = tmp_path / "table.csv"
    original = text.encode()
    path.write_bytes(original)
    parsed = TextFamilyParser().parse("source", path)
    block = next(b for b in parsed.blocks if b.text == value)
    assert block.locator["record"] == 2
    assert block.locator["column_name"] == column
    assert path.read_bytes() == original
    units, chunks = chunk_text(parsed)
    assert any(c.text == value for c in chunks)
    assert all(json.loads(u.locator_json)["decoded_text_version"] == 1 for u in units)


def test_csv_headerless_duplicate_and_ragged_columns(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text("0012,0034\n0056,0078,0090\n")
    blocks = TextFamilyParser().parse("source", path, csv_has_header=False).blocks
    assert [(b.text, b.locator["column"]) for b in blocks] == [
        ("0012", 1), ("0034", 2), ("0056", 1), ("0078", 2), ("0090", 3)]
    path.write_text("name,name\na,b\n")
    blocks = TextFamilyParser().parse("s", path, csv_has_header=True).blocks
    assert [b.locator["column"] for b in blocks if b.locator["record"] == 2] == [1, 2]


def test_html_never_fetches_or_exposes_active_content(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("network access"))
    path = tmp_path / "hostile.html"
    path.write_text('''<head><title>Secret title</title></head><h1>Safety</h1>
      <!-- INJECT COMMENT --><script>fetch('http://invalid');</script>
      <style>INJECT STYLE</style><iframe src="http://invalid">INJECT FRAME</iframe>
      <object>INJECT OBJECT</object><svg><text>INJECT SVG</text></svg>
      <p hidden>HIDDEN</p><div style="display:none">HIDDEN</div>
      <p onclick="alert(1)">Keep <a href="http://invalid">link text</a> &amp; facts.</p>
      <img src="http://invalid" alt="INJECT ALT"><h2>Details</h2><p>Answer is 42.</p>''')
    parsed = TextFamilyParser().parse("s", path)
    text = " ".join(b.text for b in parsed.blocks)
    assert text == "Safety Keep link text & facts. Details Answer is 42."
    assert parsed.blocks[-1].locator["heading_path"] == ["Safety", "Details"]


@pytest.mark.parametrize("data", [b"a\x00b\x01c", b"%PDF-1.4", b"PK\x03\x04junk", b"\x89PNG\r\n\x1a\n"])
def test_binary_disguised_as_text_rejected(tmp_path, data):
    path = tmp_path / "binary.txt"
    path.write_bytes(data)
    with pytest.raises(InvalidFileError):
        check_file_signature(path)


@pytest.mark.parametrize("suffix,text", [
    (".txt", "A line.\nAnother exact  line.\n"),
    (".md", "# Parent\nBody.\n## Child\nMore exact  words.\n```\n# code\n```\n"),
    (".html", "<h1>Parent</h1><p>Some exact words here.</p>"),
    (".csv", 'name,note\nAda,"Exact  quoted\nwords here"\n'),
])
def test_chunk_quotes_are_exact_substrings(tmp_path, suffix, text):
    class Counter:
        def count(self, text):
            return len(text)

    path = tmp_path / ("doc" + suffix)
    path.write_bytes(text.encode())
    parsed = TextFamilyParser().parse("s", path)
    units, chunks = chunk_text(parsed, counter=Counter(), max_tokens=15)
    for unit, chunk in zip(units, chunks):
        loc = json.loads(unit.locator_json)
        candidates = [b for b in parsed.blocks if all(
            b.locator.get(k) == loc.get(k) for k in ("record", "column", "block", "heading_path"))]
        assert any(b.text[loc["char_start"]:loc["char_end"]] == chunk.text for b in candidates)
        assert len(chunk.text) <= 15
    assert path.read_bytes() == text.encode()
