"""Folder suggestions: ranking, bounds, hidden/linked folders left alone."""

from __future__ import annotations

from pathlib import Path

from docket.services.sources import suggest
from docket.services.sources.scan import ScanResult
from docket.services.sources.suggest import suggest_folders


def _docs(folder: Path, n: int, ext: str = ".docx") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"f{i}{ext}").write_bytes(b"PK\x03\x04" + b"0" * 20)


def _home(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Downloads").mkdir()
    (home / "Desktop").mkdir()
    return home


def test_ranked_by_document_count_with_type_summary(tmp_path):
    home = _home(tmp_path)
    _docs(home / "Documents" / "Few", 2)
    _docs(home / "Documents" / "Many", 5)
    _docs(home / "Downloads" / "Mid", 3)
    _docs(home / "Downloads" / "Empty", 0)
    out = suggest_folders(home, cwd=home)
    assert [s.name for s in out] == ["Many", "Mid", "Few"]
    assert out[0].documents == 5 and out[0].type_summary == "5 Word"
    assert out[0].count_text == "5 documents"


def test_siblings_of_cwd_and_registered_excluded(tmp_path):
    home = _home(tmp_path)
    work = tmp_path / "work"
    _docs(work / "here", 1)
    _docs(work / "sibling", 4)
    _docs(home / "Documents" / "Taken", 9)
    out = suggest_folders(home, cwd=work / "here", registered=[str(home / "Documents" / "Taken")])
    assert [s.name for s in out] == ["sibling"]


def test_hidden_links_and_places_themselves_are_skipped(tmp_path):
    home = _home(tmp_path)
    _docs(home / "Documents", 7)  # the place itself is not a suggestion
    _docs(home / "Documents" / ".hidden", 5)
    _docs(tmp_path / "elsewhere", 5)
    (home / "Documents" / "link").symlink_to(tmp_path / "elsewhere")
    assert suggest_folders(home, cwd=Path("/")) == []


def test_never_scans_root_or_the_home_parent(tmp_path):
    seen = []

    def fake_scan(folder, **kw):
        seen.append(str(folder))
        raise AssertionError("nothing should be scanned")

    assert suggest.candidate_folders(tmp_path / "nohome", Path("/")) == []
    assert suggest.candidate_folders(tmp_path / "nohome", Path("/home/someone")) == []
    assert suggest_folders(tmp_path / "nohome", cwd=Path("/"), scan=fake_scan) == []
    assert not seen


def test_time_and_entry_budgets_stop_the_scan(tmp_path):
    home = _home(tmp_path)
    for i in range(10):
        _docs(home / "Documents" / f"d{i}", 1)
    calls = []

    def fake_scan(folder, **kw):
        calls.append(kw)
        return ScanResult(root=folder, documents=1, by_type={".pdf": 1}, entries_scanned=100)

    out = suggest_folders(home, cwd=home, max_entries=250, scan=fake_scan)
    assert len(calls) == 3 and len(out) == 3  # 250 entries / 100 each
    assert all(c["max_entries"] <= suggest.PER_FOLDER_ENTRIES and c["time_budget_s"] <= 0.6 for c in calls)
    calls.clear()
    assert suggest_folders(home, cwd=home, time_budget_s=0.0, scan=fake_scan) == []
    assert not calls


def test_limit(tmp_path):
    home = _home(tmp_path)
    for i in range(9):
        _docs(home / "Documents" / f"d{i}", i + 1)
    out = suggest_folders(home, cwd=home, limit=4)
    assert [s.documents for s in out] == [9, 8, 7, 6]
