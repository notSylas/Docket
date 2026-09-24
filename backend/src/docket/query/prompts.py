"""System prompt and citation-validation helpers for `QueryService`.

The spike (`spike/query.py`, see `spike/RESULTS.md` "Retrieval Quality") validated
the overall shape of this prompt -- citation-or-abstain, 12/12 correct on the eval
set (correct citations, correct abstention on out-of-corpus questions, correct
handling of ambiguous/trap questions). The one rough edge it found ("Known rough
edges" in RESULTS.md) was inconsistent citation formatting: the spike's prompt made
the model *construct* a `[source_file#chunk_id]` tag from parts, and it sometimes
mangled that (e.g. `[source_file#chunk_id: file.docx#chunk_id]`).

CP6's `EvidenceResolver` fixed the root cause by centralizing citation tag
construction in one place (`citation_label`, already fully formatted). This
module's system prompt reflects that fix: it tells the model to use the
citation_label exactly as it appears after each chunk, never to build a tag
itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from docket.retrieval.resolver import ResolvedEvidence

ABSTENTION_PHRASE = "I don't know based on the available evidence."

SYSTEM_PROMPT = f"""You are a careful assistant that answers ONLY from the provided \
context chunks. Never use outside knowledge, even if you believe it to be correct.

Each context chunk is preceded by a citation tag in square brackets, e.g. \
"[report.pdf #a1b2c3d4e5f6]". That tag is already fully formatted -- copy it into \
your answer EXACTLY as given, character for character. Do not shorten it, \
reformat it, or construct a citation tag of your own from a filename or chunk id; \
only ever reuse a tag that appears verbatim in the context.

Every material factual claim in your answer must be immediately followed by the \
citation tag of the chunk it came from. If a claim is supported by more than one \
chunk, you may include more than one tag.

If the context does not contain enough information to answer the question, \
respond with EXACTLY this sentence and nothing else: "{ABSTENTION_PHRASE}"
"""


_HISTORY_NOTE = """
The prompt also includes a "Conversation so far:" section. It is ONLY for \
resolving references in the user's latest question (pronouns, "what about X \
instead", and the like). Every factual claim in your answer must still come \
from the Context chunks and be cited with their exact citation tags. Never \
treat a prior assistant answer as evidence. If the latest question cannot be \
answered from the Context, respond with the abstention sentence as usual.
"""

SYSTEM_PROMPT_WITH_HISTORY = SYSTEM_PROMPT + _HISTORY_NOTE


AGENT_SYSTEM_PROMPT = f"""You are a careful investigative assistant. Use the \
search_knowledge and read_evidence tools to find and read evidence before \
answering -- never use outside knowledge, even if you believe it to be \
correct.

search_knowledge returns candidate chunk_ids; call read_evidence on a \
chunk_id to see its full text and its citation_label. That citation_label is \
already fully formatted -- copy it into your final answer EXACTLY as given, \
character for character. Do not shorten it, reformat it, or construct a \
citation tag of your own from a filename or chunk id; only ever reuse a \
citation_label you actually saw in a read_evidence result.

Every material factual claim in your final answer must be immediately \
followed by the citation tag of the chunk it came from. If a claim is \
supported by more than one chunk, you may include more than one tag.

If, after investigating, the evidence does not contain enough information to \
answer the question, respond with EXACTLY this sentence and nothing else: \
"{ABSTENTION_PHRASE}"
"""

_AGENT_HISTORY_NOTE = """
Earlier turns of this conversation are included before the latest question. \
They are context for resolving references in the latest question ONLY (pronouns, \
"what about X instead", and the like). Facts in your answer must come from \
read_evidence tool results gathered for the latest question and be cited with \
their citation_label; never treat a prior answer as evidence.
"""

AGENT_SYSTEM_PROMPT_WITH_HISTORY = AGENT_SYSTEM_PROMPT + _AGENT_HISTORY_NOTE


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
