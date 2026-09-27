"""Real-PDF regression for `docling_wrapper._insert_page_markers`.

Checkpoint 1 of visual retrieval (page-number provenance on chunks, commit
`1251ebb`) shipped with only synthetic marker tests, which all used short
placeholder strings. Against a real, long NCERT physics chapter PDF, that
version's forward-substring matcher stalled after ~7 of 26 real pages: short,
generic, per-page-repeating anchors (single-letter formula labels, running
headers, "EXAMPLE 3.1"-style captions) could land the shared forward search
cursor on the wrong, much-too-far-ahead occurrence, after which every later
page silently stopped matching. Synthetic unit tests never exercise text at
this scale or repetition, so this class of bug is only really caught against
a real multi-page document -- hence this test, separate from
`tests/unit/test_docling_wrapper.py`.

Marked `integration`: it loads real Docling models and parses a real PDF
(no Ollama needed, unlike most `integration`-marked tests, but still slow
and heavy -- not something every unit test run should pay for). Skips
entirely when the real corpus isn't present, since `Reference_Books/` holds
copyrighted NCERT textbooks and is deliberately gitignored, not part of the
repo (see the repo's own `.gitignore` and commit history) -- this test can
only ever run on a machine that has that folder populated locally.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
PHYSICS_ZIP = REPO_ROOT / "Reference_Books" / "leph1dd.zip"

# A page with no distinctively-matchable prose (an all-table/all-formula
# page) is a legitimate, expected miss -- not every one of the real
# document's pages needs a marker. But the original bug's failure mode was
# categorical (everything past an early stall point, not occasional misses),
# so a healthy fix should comfortably clear this bar on ordinary chapter
# content. 0.85 was chosen as a real, checked number: the fix that motivated
# this test hits 26/26 (1.00) and 28/29 (0.97) on the two chapters checked
# during development -- 0.85 leaves headroom for legitimate page-image-only
# misses without masking a real regression back toward the old ~0.27 (7/26).
_MIN_COVERAGE_RATIO = 0.85


@pytest.mark.skipif(not PHYSICS_ZIP.exists(), reason="Reference_Books/ physics corpus not present locally")
def test_page_markers_cover_most_real_pages(tmp_path: Path) -> None:
    from docket.infra.parsing.docling_wrapper import DoclingParser

    with zipfile.ZipFile(PHYSICS_ZIP) as zf:
        zf.extract("leph103.pdf", tmp_path)
    pdf_path = tmp_path / "leph103.pdf"

    parser = DoclingParser()
    parsed = parser.parse("src-test", pdf_path)

    assert parsed.text_with_page_markers is not None
    markers = re.findall(r"<!--PAGE:(\d+)-->", parsed.text_with_page_markers)
    total_pages = max(parsed.page_images) if parsed.page_images else 0

    assert total_pages > 20, "fixture is expected to be a real multi-page chapter"
    coverage = len(set(markers)) / total_pages
    assert coverage >= _MIN_COVERAGE_RATIO, (
        f"only {len(set(markers))}/{total_pages} real pages got a marker "
        f"({coverage:.2f} < {_MIN_COVERAGE_RATIO}) -- page attribution is "
        "stalling on this real document again"
    )

    # Markers must still never leak into the marker-free `text`.
    assert "<!--PAGE:" not in parsed.text
