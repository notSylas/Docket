"""System prompt and per-chunk drafting prompt for `eval.draft`.

Moved from `eval/draft.py`'s `DRAFT_SYSTEM`/`draft_prompt`. `DRAFT_SYSTEM` is
recomposed with `shared.JSON_ONLY_REPLY`, the exact clause it and
`prompts.judge.JUDGE_SYSTEM` already shared verbatim before this move.
"""

from __future__ import annotations

from typing import Protocol

from docket.prompts.shared import JSON_ONLY_REPLY

MAX_PROMPT_CHUNK_CHARS = 3000

DRAFT_SYSTEM = (
    "You write evaluation questions for a document question-answering system. " + JSON_ONLY_REPLY
)


class _ChunkLike(Protocol):
    """Minimal structural type for `draft_prompt`'s argument.

    Deliberately duplicated from `eval.draft.ChunkLike` rather than
    imported: `eval.draft` imports `DRAFT_SYSTEM`/`draft_prompt` from this
    module, so an import the other way would be circular. `eval.draft`'s
    version is the canonical one; this one only needs to describe the
    fields `draft_prompt` itself reads.
    """

    source_name: str
    heading: str | None
    text: str


def draft_prompt(chunk: _ChunkLike) -> str:
    where = chunk.source_name + (f" > {chunk.heading}" if chunk.heading else "")
    return (
        f"Passage (from {where}):\n\"\"\"\n{chunk.text[:MAX_PROMPT_CHUNK_CHARS]}\n\"\"\"\n\n"
        "Write ONE question that a reader could answer from this passage alone. Rules:\n"
        "- The question must be self-contained: never say 'the passage', 'the text' or 'the table'.\n"
        "- It must ask for a concrete fact (a value, name, date, count, or a list of items), not an opinion.\n"
        '- "quote": copy 20-60 characters from the passage EXACTLY, character for character, '
        "the words that contain the answer. Do not paraphrase or fix typos.\n"
        '- "facts": 1-4 short strings a correct answer must contain (exact numbers, names, values; '
        "for a list question, one string per item).\n"
        '- "type": one of single_fact, table_lookup, enumeration, numeric.\n'
        'If the passage holds no concrete answerable fact, reply {"skip": true}.\n'
        'Reply as JSON: {"question": "...", "quote": "...", "facts": ["..."], "type": "..."}'
    )
