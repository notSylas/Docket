"""Context-block formatting and citation validation for `QueryService`.

Moved from `query/prompts.py` (`build_context_block`, `_CITATION_SHAPED_RE`,
`ValidationResult`, `validate_citations`). This is prompt-*adjacent* logic
-- no LLM ever sees this code, only its output/input -- so it lives here
rather than in `docket.prompts`, which is reserved for actual model-facing
text. `query/prompts.py` is now a thin re-export shim for the prompts
package; `query/service.py` imports the names below from this module
directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from docket.prompts.shared import ABSTENTION_PHRASE
from docket.retrieval.resolver import ResolvedEvidence


def build_context_block(resolved_chunks: list[ResolvedEvidence]) -> str:
    """Formats resolved evidence into the context block the model sees.

    Each chunk is rendered as its citation_label on its own line, immediately
    followed by its text -- e.g.::

        [report.pdf #a1b2c3d4e5f6]
        This is the chunk's text.

    Chunks are separated by a blank line, so it's unambiguous to the model
    (and to a human reading the prompt) which label pairs with which text
    block. Same shape as the spike's `context_blocks` construction in
    `spike/query.py`, just built from `ResolvedEvidence` instead of raw DB rows.
    """
    blocks = [f"{chunk.citation_label}\n{chunk.text}" for chunk in resolved_chunks]
    return "\n\n".join(blocks)


# Matches a bracketed substring that "looks like" a citation tag in this
# codebase's format -- i.e. it contains a "#" somewhere inside the brackets,
# the one structural feature every real `citation_label` has (see
# `docket.retrieval.resolver._citation_label`). Restricting to "contains a
# hash" (rather than matching any `[...]`) avoids flagging ordinary bracketed
# asides in the model's prose (e.g. "[roughly]" or "[1]") as fabricated
# citations. The accepted false-positive risk: a bracketed aside that happens
# to contain a literal "#" character (uncommon in prose) would still be
# flagged as an unknown citation.
_CITATION_SHAPED_RE = re.compile(r"\[[^\[\]]*#[^\[\]]*\]")


@dataclass(frozen=True)
class ValidationResult:
    is_abstention: bool
    cited_labels: list[str]
    uncited: bool
    unknown_citations: list[str]


def validate_citations(answer: str, resolved_chunks: list[ResolvedEvidence]) -> ValidationResult:
    """Checks a generated answer against the citation_labels that were
    actually available in the retrieved context.

    Rules (exact, so behavior is predictable/testable):
    - `is_abstention`: True iff `answer.strip() == ABSTENTION_PHRASE` exactly
      (no fuzzy/startswith matching -- the system prompt asks for the phrase
      verbatim and nothing else, so an exact match is the correct check; a
      near-miss should NOT be silently treated as abstention).
    - `cited_labels`: the subset of `resolved_chunks`' citation_labels that
      appear as an exact substring of `answer`, in the order the chunks were
      given (not the order they appear in `answer`).
    - `uncited`: True iff the answer is not an abstention, has non-trivial
      content (non-empty after stripping), and `cited_labels` is empty --
      i.e. the model made claims but attributed none of them.
    - `unknown_citations`: bracket-shaped substrings of `answer` (see
      `_CITATION_SHAPED_RE`) that do not exactly match any of
      `resolved_chunks`'s citation_labels -- a sign the model fabricated or
      mangled a citation instead of reusing a real one verbatim.
    """
    stripped = answer.strip()
    is_abstention = stripped == ABSTENTION_PHRASE

    valid_labels = {chunk.citation_label for chunk in resolved_chunks}
    cited_labels = [
        chunk.citation_label for chunk in resolved_chunks if chunk.citation_label in answer
    ]

    uncited = not is_abstention and bool(stripped) and not cited_labels

    found_tags = set(_CITATION_SHAPED_RE.findall(answer))
    unknown_citations = sorted(found_tags - valid_labels)

    return ValidationResult(
        is_abstention=is_abstention,
        cited_labels=cited_labels,
        uncited=uncited,
        unknown_citations=unknown_citations,
    )
